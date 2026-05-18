"""End-to-end: boot Hummingbird with HUMMINGBIRD_PLUGIN=
openapis_dodp, intercept the DODP SOAP traffic with
httpx.MockTransport, then hit Hummingbird's REST surface with
Basic auth and verify the bookshelf flows through this plugin.

Loads hummingbird via FastAPI's TestClient; we don't start the
uvicorn server. Auth cache is reset before each test so the
plugin's authenticate() actually runs.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from openapis_ca_dodp import sessions
from openapis_ca_dodp.client import DEFAULT_DODP_NS, DodpClient

from tests.conftest import authenticated_handshake, soap_envelope


DNS = DEFAULT_DODP_NS


def _basic_auth(user: str, pw: str) -> dict[str, str]:
    raw = f"{user}:{pw}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def hb_app(monkeypatch):
    """Boot a fresh hummingbird app with the plugin selected
    via env. Reset the auth cache + plugin loader so the
    selection takes effect for THIS test."""
    monkeypatch.setenv("HUMMINGBIRD_PLUGIN", "openapis_dodp")
    monkeypatch.setenv("OPENAPIS_DODP_BASE_URL", "https://dodp.example/svc")

    # Both hummingbird and this plugin pin their settings as
    # module-level singletons constructed at import time. Mutate
    # the live object so any consumer that already imported it
    # (hummingbird.plugins did) sees the new value.
    import hummingbird.config as hb_cfg
    hb_cfg.settings.plugin = "openapis_dodp"

    import openapis_ca_dodp.config as cfg
    cfg.settings.base_url = "https://dodp.example/svc"

    import hummingbird.plugins as P
    P._active = None
    P._loaded = False

    # Hummingbird's Basic-auth cache also persists across the
    # request lifecycle; clear it so each test exercises a real
    # call into plugin.authenticate() the first time.
    import hummingbird.auth as A
    A._VALIDATED.clear()

    from hummingbird.main import app
    return app


def test_bookshelf_flows_through_plugin(hb_app, monkeypatch):
    """One round-trip through every layer:
    - TestClient sends HTTP GET /bookshelf/list with Basic auth
    - hummingbird's auth dependency calls plugin.authenticate
    - the plugin issues SOAP logOn against our MockTransport
    - hummingbird's REST handler calls plugin.list_bookshelf
    - the plugin issues SOAP getContentList
    - response surfaces back as JSON.
    """
    calls: list[str] = []

    def dodp_handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "logOn" in body:
            calls.append("logOn")
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<logOnResponse xmlns="{DNS}">'
                    f'<logOnResult>true</logOnResult>'
                    f'</logOnResponse>'
                ),
            )
        if "getContentList" in body:
            calls.append("getContentList")
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentListResponse xmlns="{DNS}">'
                    f'  <contentList id="issued">'
                    f'    <contentItem id="dodp-book-7">'
                    f'      <label><text>End-to-End Title</text></label>'
                    f'    </contentItem>'
                    f'  </contentList>'
                    f'</getContentListResponse>'
                ),
            )
        raise AssertionError(f"unexpected SOAP body: {body[:80]}")

    # Patch every httpx.AsyncClient the plugin spins up to use
    # our MockTransport. Same trick as the unit tests. The
    # handshake wrapper transparently answers the post-logOn
    # service-attributes + reading-system-attributes calls.
    transport = httpx.MockTransport(authenticated_handshake(dodp_handler))
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    sessions._clear_for_tests()
    client = TestClient(hb_app)
    r = client.get(
        "/protocols/hummingbird/v1/bookshelf/list",
        headers=_basic_auth("alice", "hunter2"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Hummingbird's BookshelfListResponse wraps the books in a
    # field; the exact shape can vary slightly between versions
    # but we just need the title to appear somewhere.
    payload = repr(body)
    assert "End-to-End Title" in payload
    # And we should see both SOAP ops actually went over the
    # wire -- this is the load-bearing assertion that the auth
    # path runs the plugin (not a cached env-credential).
    assert calls == ["logOn", "getContentList"]


def test_unconfigured_plugin_returns_empty_bookshelf(monkeypatch):
    """Operator boots hummingbird with the plugin selected but
    forgot to set OPENAPIS_DODP_BASE_URL. Plugin loads, logs a
    warning, and every hook returns its empty fallback so the
    service still serves coherently."""
    monkeypatch.setenv("HUMMINGBIRD_PLUGIN", "openapis_dodp")
    monkeypatch.delenv("OPENAPIS_DODP_BASE_URL", raising=False)

    import hummingbird.plugins as P
    P._active = None
    P._loaded = False
    import hummingbird.auth as A
    A._VALIDATED.clear()
    import openapis_ca_dodp.config as cfg
    cfg.settings = cfg.Settings()
    # Force the plugin instance held in P._active to use the
    # newly-rebuilt settings (the cls()-instantiation reads
    # the module-level settings at construction time).

    from hummingbird.main import app
    sessions._clear_for_tests()
    client = TestClient(app)
    # Without a configured backend, plugin.authenticate returns
    # False, so the request 401s. We don't get a stacktrace,
    # which is the load-bearing change we just shipped.
    r = client.get(
        "/protocols/hummingbird/v1/bookshelf/list",
        headers=_basic_auth("alice", "pw"),
    )
    assert r.status_code == 401


def test_dodp_client_used_as_module_singleton(monkeypatch):
    """Quick sanity: when the plugin __init__ is called by
    Hummingbird's loader (no test injection), the DodpClient is
    built from settings -- not None. The previous version
    crashed at instantiation; this pins that fix."""
    monkeypatch.setenv("OPENAPIS_DODP_BASE_URL", "https://dodp.example/svc")
    import openapis_ca_dodp.config as cfg
    cfg.settings = cfg.Settings()
    from openapis_ca_dodp.plugin import OpenapisDodpPlugin
    p = OpenapisDodpPlugin()
    assert isinstance(p._client, DodpClient)
    assert p._client.base_url == "https://dodp.example/svc"
