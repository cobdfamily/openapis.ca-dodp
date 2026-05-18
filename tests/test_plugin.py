"""Plugin-level tests: wire ``OpenapisDodpPlugin`` against a
mocked DODP server and assert the hummingbird-side contract.

Tests exercise the hooks Hummingbird actually calls:
authenticate -> list_bookshelf -> add/remove -> bookmarks
-> download. Each test sets up a DODP-response handler scoped
to that hook so we don't accidentally answer the wrong call
shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from openapis_ca_dodp import sessions
from openapis_ca_dodp.client import DEFAULT_DODP_NS, DodpClient
from openapis_ca_dodp.plugin import OpenapisDodpPlugin

from tests.conftest import soap_envelope, soap_fault


DNS = DEFAULT_DODP_NS
CLIENT_URL = "https://dodp.example.org/service"


def _plugin(handler) -> tuple[OpenapisDodpPlugin, httpx.MockTransport]:
    """Build a plugin whose client points at the mocked DODP URL.
    The plugin internally builds its own httpx.AsyncClient on
    authenticate(); we intercept by passing the same transport
    via a monkey-patched factory.
    """
    transport = httpx.MockTransport(handler)
    client = DodpClient(base_url=CLIENT_URL, namespace=DNS)
    plugin = OpenapisDodpPlugin(client=client)
    return plugin, transport


@pytest.fixture
def install_transport(monkeypatch):
    """Patch ``httpx.AsyncClient`` so every instance the plugin
    spins up under authenticate() uses our MockTransport. This
    is the cleanest way to inject without rewriting the plugin
    to take a session factory."""
    holder: dict[str, httpx.MockTransport] = {}

    def _set(transport: httpx.MockTransport) -> None:
        holder["t"] = transport

        original = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    return _set


# -- authenticate -------------------------------------------


async def test_authenticate_stores_session(install_transport):
    def handler(_: httpx.Request) -> httpx.Response:
        body = (
            f'<logOnResponse xmlns="{DNS}">'
            f'<logOnResult>true</logOnResult>'
            f'</logOnResponse>'
        )
        return httpx.Response(200, text=soap_envelope(body))

    plugin, transport = _plugin(handler)
    install_transport(transport)
    ok = await plugin.authenticate("alice", "hunter2")
    assert ok is True
    # Session was stored against the hummingbird user.
    assert sessions.get("alice") is not None


async def test_authenticate_bad_credentials_returns_false(install_transport):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=soap_fault("Invalid credentials"))

    plugin, transport = _plugin(handler)
    install_transport(transport)
    ok = await plugin.authenticate("alice", "bad")
    assert ok is False
    # No session left behind on auth failure -- the next request
    # for this user starts fresh.
    assert sessions.get("alice") is None


async def test_authenticate_replaces_existing_session(install_transport):
    """A second logOn for the same user must drop the previous
    session (and close its underlying httpx client) before
    starting a fresh one. Otherwise we leak cookies/sockets
    across credential rotations."""
    def handler(_: httpx.Request) -> httpx.Response:
        body = (
            f'<logOnResponse xmlns="{DNS}">'
            f'<logOnResult>true</logOnResult>'
            f'</logOnResponse>'
        )
        return httpx.Response(200, text=soap_envelope(body))

    plugin, transport = _plugin(handler)
    install_transport(transport)
    await plugin.authenticate("alice", "first")
    first_session = sessions.get("alice")
    await plugin.authenticate("alice", "second")
    second_session = sessions.get("alice")
    assert first_session is not None and second_session is not None
    assert first_session is not second_session


# -- list_bookshelf -----------------------------------------


async def test_list_bookshelf_returns_empty_without_session(install_transport):
    # No authenticate() call -> no session in the registry.
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("DODP server should not be called")

    plugin, transport = _plugin(handler)
    install_transport(transport)
    books = await plugin.list_bookshelf("alice")
    assert books == []


async def test_list_bookshelf_maps_dodp_ids_to_ints(install_transport):
    """Two DODP contentIDs should round-trip through the id map
    and surface as small ints in the BookRecord. The plugin must
    keep the same int across subsequent list_bookshelf calls so
    add/remove_to_bookshelf can refer back."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "logOn" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<logOnResponse xmlns="{DNS}">'
                    f'<logOnResult>true</logOnResult>'
                    f'</logOnResponse>'
                ),
            )
        if "getContentList" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentListResponse xmlns="{DNS}">'
                    f'  <contentList id="issued">'
                    f'    <contentItem id="con-a">'
                    f'      <label><text>Title A</text></label>'
                    f'    </contentItem>'
                    f'    <contentItem id="con-b">'
                    f'      <label><text>Title B</text></label>'
                    f'    </contentItem>'
                    f'  </contentList>'
                    f'</getContentListResponse>'
                ),
            )
        raise AssertionError(f"unexpected SOAP body: {body[:80]}")

    plugin, transport = _plugin(handler)
    install_transport(transport)
    assert await plugin.authenticate("alice", "pw") is True
    books1 = await plugin.list_bookshelf("alice")
    books2 = await plugin.list_bookshelf("alice")
    assert {b.id for b in books1} == {b.id for b in books2}
    assert [b.title for b in books1] == ["Title A", "Title B"]


async def test_list_bookshelf_auth_fault_drops_session(install_transport):
    """If the DODP server tells us our session is gone we MUST
    drop the cached session. Otherwise subsequent calls keep
    sending the dead cookie until the user manually re-auths."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
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
                text=soap_fault("not logged in"),
            )
        raise AssertionError(body[:80])

    plugin, transport = _plugin(handler)
    install_transport(transport)
    await plugin.authenticate("alice", "pw")
    assert sessions.get("alice") is not None
    books = await plugin.list_bookshelf("alice")
    assert books == []
    assert sessions.get("alice") is None


# -- add / remove -------------------------------------------


async def test_add_to_bookshelf_uses_id_map(install_transport):
    issued_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "logOn" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<logOnResponse xmlns="{DNS}">'
                    f'<logOnResult>true</logOnResult>'
                    f'</logOnResponse>'
                ),
            )
        if "getContentList" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentListResponse xmlns="{DNS}">'
                    f'  <contentList id="issued">'
                    f'    <contentItem id="con-xyz">'
                    f'      <label><text>Title</text></label>'
                    f'    </contentItem>'
                    f'  </contentList>'
                    f'</getContentListResponse>'
                ),
            )
        if "issueContent" in body:
            match = re.search(r"<[^>]*contentID[^>]*>([^<]+)<", body)
            assert match
            issued_ids.append(match.group(1))
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<issueContentResponse xmlns="{DNS}">'
                    f'<issueContentResult>true</issueContentResult>'
                    f'</issueContentResponse>'
                ),
            )
        raise AssertionError(body[:80])

    plugin, transport = _plugin(handler)
    install_transport(transport)
    await plugin.authenticate("alice", "pw")
    books = await plugin.list_bookshelf("alice")
    int_id = books[0].id
    ok = await plugin.add_to_bookshelf("alice", int_id)
    assert ok is True
    assert issued_ids == ["con-xyz"]


async def test_remove_to_bookshelf_with_unknown_int_lazily_refreshes(install_transport):
    """If Hummingbird's auth cache outlives a process restart it
    can call remove with an int the new process never minted.
    The plugin should refresh the list once and retry; if the
    id still isn't there, return False."""
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        body = request.content.decode()
        if "logOn" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<logOnResponse xmlns="{DNS}">'
                    f'<logOnResult>true</logOnResult>'
                    f'</logOnResponse>'
                ),
            )
        if "getContentList" in body:
            list_calls += 1
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentListResponse xmlns="{DNS}">'
                    f'  <contentList id="issued">'
                    f'    <contentItem id="con-known">'
                    f'      <label><text>Title</text></label>'
                    f'    </contentItem>'
                    f'  </contentList>'
                    f'</getContentListResponse>'
                ),
            )
        if "returnContent" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<returnContentResponse xmlns="{DNS}">'
                    f'<returnContentResult>true</returnContentResult>'
                    f'</returnContentResponse>'
                ),
            )
        raise AssertionError(body[:80])

    plugin, transport = _plugin(handler)
    install_transport(transport)
    await plugin.authenticate("alice", "pw")
    # Caller passes an int we never minted -> refresh fires -> still missing -> False.
    ok = await plugin.remove_from_bookshelf("alice", 999)
    assert ok is False
    assert list_calls == 1


# -- search -------------------------------------------------


async def test_search_returns_empty_without_calling_dodp(install_transport):
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("DODP server should not be called for search")

    plugin, transport = _plugin(handler)
    install_transport(transport)
    result = await plugin.search("alice", "query", None, page=1)
    assert result.books == []
    assert result.total_results == 0


# -- bookmarks ----------------------------------------------


async def test_set_bookmark_and_get_bookmark(install_transport):
    stored: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "logOn" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<logOnResponse xmlns="{DNS}">'
                    f'<logOnResult>true</logOnResult>'
                    f'</logOnResponse>'
                ),
            )
        if "getContentList" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentListResponse xmlns="{DNS}">'
                    f'  <contentList id="issued">'
                    f'    <contentItem id="con-bm"><label><text>X</text></label></contentItem>'
                    f'  </contentList>'
                    f'</getContentListResponse>'
                ),
            )
        if "setBookmarks" in body:
            match = re.search(r"<[^>]*ncxRef[^>]*>([^<]+)<", body)
            if match:
                stored["ncxRef"] = match.group(1)
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<setBookmarksResponse xmlns="{DNS}">'
                    f'<setBookmarksResult>true</setBookmarksResult>'
                    f'</setBookmarksResponse>'
                ),
            )
        if "getBookmarks" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getBookmarksResponse xmlns="{DNS}">'
                    f'  <bookmarkSet>'
                    f'    <lastmark><ncxRef>part-2</ncxRef></lastmark>'
                    f'  </bookmarkSet>'
                    f'</getBookmarksResponse>'
                ),
            )
        raise AssertionError(body[:80])

    plugin, transport = _plugin(handler)
    install_transport(transport)
    await plugin.authenticate("alice", "pw")
    books = await plugin.list_bookshelf("alice")
    int_id = books[0].id
    ok = await plugin.set_bookmark(
        "alice", int_id, {"lastmark": {"ncxRef": "part-2"}},
    )
    assert ok is True
    assert stored["ncxRef"] == "part-2"
    fetched = await plugin.get_bookmark("alice", int_id)
    # Element-to-dict surface: the body is the <bookmarkSet> wrap.
    assert "bookmarkSet" in fetched


# -- download -----------------------------------------------


async def test_download_writes_cache_file(install_transport, tmp_path: Path):
    payload = b"ID3audio_bytes_here"

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "logOn" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<logOnResponse xmlns="{DNS}">'
                    f'<logOnResult>true</logOnResult>'
                    f'</logOnResponse>'
                ),
            )
        if "getContentList" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentListResponse xmlns="{DNS}">'
                    f'  <contentList id="issued">'
                    f'    <contentItem id="con-dl">'
                    f'      <label><text>DL</text></label>'
                    f'    </contentItem>'
                    f'  </contentList>'
                    f'</getContentListResponse>'
                ),
            )
        if "getContentResources" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getContentResourcesResponse xmlns="{DNS}">'
                    f'  <resources>'
                    f'    <resource uri="https://cdn.example/file.mp3" mimeType="audio/mpeg" size="123"/>'
                    f'    <resource uri="https://cdn.example/file.ncx" mimeType="application/x-dtbncx+xml"/>'
                    f'  </resources>'
                    f'</getContentResourcesResponse>'
                ),
            )
        # The audio resource fetch:
        if request.url.path.endswith(".mp3"):
            return httpx.Response(200, content=payload)
        raise AssertionError(f"unexpected: {request.url}")

    plugin, transport = _plugin(handler)
    install_transport(transport)
    await plugin.authenticate("alice", "pw")
    books = await plugin.list_bookshelf("alice")
    int_id = books[0].id

    cache = tmp_path / "downloads"
    out = await plugin.download(
        "alice", fmt=12000, node_id=int_id, cache_dir=cache,
    )
    assert out is not None
    assert out.exists()
    assert out.read_bytes() == payload
    assert out.suffix == ".mp3"
