"""DODP SOAP client unit tests.

Pins the wire shape: each method builds a SOAP envelope whose
body holds the operation name + parameters in the DODP
namespace, and each response is parsed by element-local-name +
namespace so we tolerate Response-wrapper / no-wrapper variants
and v1 vs v2 inner element differences.
"""

from __future__ import annotations

import httpx
import pytest
from lxml import etree

from openapis_ca_dodp.client import (
    DEFAULT_DODP_NS,
    DodpAuthFault,
    DodpClient,
    DodpFault,
)

from tests.conftest import soap_envelope, soap_fault


CLIENT_URL = "https://dodp.example.org/service"
DNS = DEFAULT_DODP_NS


def _client() -> DodpClient:
    return DodpClient(base_url=CLIENT_URL, namespace=DNS)


def _parse_body(request: httpx.Request) -> etree._Element:
    return etree.fromstring(request.content)


# -- logOn ---------------------------------------------------


async def test_log_on_returns_true(make_http):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["soapaction"] = request.headers.get("SOAPAction")
        body = (
            f'<logOnResponse xmlns="{DNS}">'
            f'<logOnResult>true</logOnResult>'
            f'</logOnResponse>'
        )
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        ok = await _client().log_on(http, "alice", "hunter2")

    assert ok is True
    assert captured["soapaction"] == '"/logOn"'
    root = etree.fromstring(captured["body"])
    # Verify the body holds a <logOn> element in the DODP NS
    # with username + password children. This is the load-bearing
    # contract; the server side reads exactly these.
    log_on_el = root.find(
        f'.//{{{DNS}}}logOn',
    )
    assert log_on_el is not None
    assert log_on_el.findtext(f'{{{DNS}}}username') == "alice"
    assert log_on_el.findtext(f'{{{DNS}}}password') == "hunter2"


async def test_log_on_returns_false(make_http):
    def handler(_: httpx.Request) -> httpx.Response:
        body = (
            f'<logOnResponse xmlns="{DNS}">'
            f'<logOnResult>false</logOnResult>'
            f'</logOnResponse>'
        )
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        ok = await _client().log_on(http, "alice", "bad")
    assert ok is False


async def test_log_on_fault_with_auth_phrasing_raises_auth_fault(make_http):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=soap_fault("Invalid credentials"),
        )

    async with make_http(handler) as http:
        with pytest.raises(DodpAuthFault) as excinfo:
            await _client().log_on(http, "alice", "bad")
    assert "Invalid credentials" in str(excinfo.value)


async def test_unrelated_fault_raises_plain_fault(make_http):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=soap_fault("disk full"))

    async with make_http(handler) as http:
        with pytest.raises(DodpFault) as excinfo:
            await _client().get_content_list(http, "issued")
    # Disk full doesn't match an auth keyword -> base class only,
    # not the auth subclass.
    assert not isinstance(excinfo.value, DodpAuthFault)
    assert "disk full" in str(excinfo.value)


# -- getContentList -----------------------------------------


async def test_get_content_list_parses_items(make_http):
    body = (
        f'<getContentListResponse xmlns="{DNS}">'
        f'  <contentList id="issued" firstItem="1" lastItem="2" totalItems="2">'
        f'    <contentItem id="con-1">'
        f'      <label><text>Book One</text></label>'
        f'      <lastModifiedDate>2026-05-01T12:00:00Z</lastModifiedDate>'
        f'    </contentItem>'
        f'    <contentItem id="con-2">'
        f'      <label><text>Book Two</text></label>'
        f'    </contentItem>'
        f'  </contentList>'
        f'</getContentListResponse>'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        items = await _client().get_content_list(http, "issued")

    assert [(i.content_id, i.label) for i in items] == [
        ("con-1", "Book One"),
        ("con-2", "Book Two"),
    ]
    assert items[0].last_modified_date == "2026-05-01T12:00:00Z"


async def test_get_content_list_tolerates_label_without_text_wrapper(make_http):
    # Some DODP v1 servers put the label string directly inside
    # <label> with no <text> child. The client must accept both.
    body = (
        f'<getContentListResponse xmlns="{DNS}">'
        f'  <contentList id="issued">'
        f'    <contentItem id="con-7">'
        f'      <label>Plain Label</label>'
        f'    </contentItem>'
        f'  </contentList>'
        f'</getContentListResponse>'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        items = await _client().get_content_list(http, "issued")
    assert items[0].label == "Plain Label"


# -- getContentResources ------------------------------------


async def test_get_content_resources_parses_resources(make_http):
    body = (
        f'<getContentResourcesResponse xmlns="{DNS}">'
        f'  <resources>'
        f'    <resource uri="https://cdn.example/a.mp3" mimeType="audio/mpeg" size="1024"/>'
        f'    <resource uri="https://cdn.example/a.ncx" mimeType="application/x-dtbncx+xml"/>'
        f'  </resources>'
        f'</getContentResourcesResponse>'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        resources = await _client().get_content_resources(http, "con-1")

    assert len(resources) == 2
    assert resources[0].uri == "https://cdn.example/a.mp3"
    assert resources[0].mime_type == "audio/mpeg"
    assert resources[0].size_bytes == 1024
    assert resources[1].size_bytes is None


# -- issueContent / returnContent ---------------------------


async def test_issue_content_returns_true(make_http):
    def handler(_: httpx.Request) -> httpx.Response:
        body = (
            f'<issueContentResponse xmlns="{DNS}">'
            f'<issueContentResult>true</issueContentResult>'
            f'</issueContentResponse>'
        )
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        ok = await _client().issue_content(http, "con-1")
    assert ok is True


async def test_return_content_returns_false_without_result_element(make_http):
    # Edge case: server omits the *Result wrapper. Our client
    # treats that as "fault-less so True" -- pin the behavior so
    # we don't accidentally regress to False on real servers.
    def handler(_: httpx.Request) -> httpx.Response:
        body = f'<returnContentResponse xmlns="{DNS}"/>'
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        ok = await _client().return_content(http, "con-1")
    assert ok is True


# -- error paths --------------------------------------------


async def test_http_500_with_no_xml_raises_dodp_fault(make_http):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    async with make_http(handler) as http:
        with pytest.raises(DodpFault) as excinfo:
            await _client().get_content_list(http, "issued")
    assert "500" in str(excinfo.value) or "HTTP" in str(excinfo.value)


async def test_transport_error_raises_dodp_fault():
    # Force a transport-level failure by pointing httpx at an
    # always-error transport. Confirms _call wraps the exception
    # in a DodpFault rather than letting httpx.HTTPError leak.
    def always_fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(always_fail),
    ) as http:
        with pytest.raises(DodpFault) as excinfo:
            await _client().log_on(http, "x", "y")
    assert "transport error" in str(excinfo.value)


def test_base_url_required():
    with pytest.raises(ValueError):
        DodpClient(base_url="")


# -- handshake methods (v0.2) -------------------------------


async def test_set_reading_system_attributes_envelope_shape(make_http):
    """The reading-system body must contain a nested
    <readingSystemAttributes> element with <manufacturer>,
    <model>, <version>, plus <config> with <supportedMimeTypes>
    that holds <mimeType> children (singular tag, NOT
    <supportedMimeTypes>X).
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        body = (
            f'<setReadingSystemAttributesResponse xmlns="{DNS}">'
            f'<setReadingSystemAttributesResult>true</setReadingSystemAttributesResult>'
            f'</setReadingSystemAttributesResponse>'
        )
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        ok = await _client().set_reading_system_attributes(
            http,
            manufacturer="cobdfamily",
            model="hummingbird",
            version="0.3.2",
        )
    assert ok is True
    sent = captured["body"]
    # Spot-check the wire shape -- mismatches here are the
    # most common reason KADOS faults the next call.
    assert "<readingSystemAttributes" in sent or ":readingSystemAttributes" in sent
    assert "cobdfamily" in sent
    assert "hummingbird" in sent
    assert "<mimeType" in sent or ":mimeType" in sent
    # The plural wrapper element should appear exactly once
    # (not as a repeated parent for each value).
    assert sent.count("supportedMimeTypes>") == 2  # opening + closing


async def test_get_service_attributes_returns_dict(make_http):
    """getServiceAttributes is a "logged for ops" call; the
    plugin doesn't gate on its result. Just confirm the client
    returns a dict so the plugin's summariser has a stable
    type to inspect."""
    body = (
        f'<getServiceAttributesResponse xmlns="{DNS}">'
        f'  <serviceAttributes>'
        f'    <service id="kados"><label><text>KADOS</text></label></service>'
        f'    <supportsSearch>true</supportsSearch>'
        f'    <supportsServerSideBack>false</supportsServerSideBack>'
        f'    <supportedOptionalOperations>SET_BOOKMARKS</supportedOptionalOperations>'
        f'    <supportedOptionalOperations>GET_BOOKMARKS</supportedOptionalOperations>'
        f'  </serviceAttributes>'
        f'</getServiceAttributesResponse>'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=soap_envelope(body))

    async with make_http(handler) as http:
        caps = await _client().get_service_attributes(http)
    assert isinstance(caps, dict)
    # Both repeated <supportedOptionalOperations> children
    # collapse to a list under _element_to_dict's same-tag rule.
    inner = caps["serviceAttributes"]
    assert isinstance(inner["supportedOptionalOperations"], list)
    assert "SET_BOOKMARKS" in inner["supportedOptionalOperations"]
