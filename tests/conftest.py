"""Shared test fixtures + canned SOAP response helpers.

The DODP server is stubbed via ``httpx.MockTransport``: the test
declares a request-handler callable that inspects the SOAP body
and returns a canned response. This keeps tests hermetic (no
network, no KADOS instance needed) while exercising both the
SOAP serialiser and parser end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


# A request-handler is a function that takes an httpx.Request and
# returns an httpx.Response. Tests pass these into the fixture
# below to wire up canned responses.
SoapHandler = Callable[[httpx.Request], httpx.Response]


def soap_envelope(body_xml: str) -> str:
    """Wrap a DODP body fragment in the surrounding SOAP envelope
    the spec mandates. The body fragment goes inside <soap:Body>
    as-is, so it should include the DODP namespace declaration on
    its outermost element."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        f'{body_xml}'
        '</soap:Body>'
        '</soap:Envelope>'
    )


def soap_fault(faultstring: str, faultcode: str = "Server") -> str:
    return soap_envelope(
        f'<soap:Fault xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<faultcode>{faultcode}</faultcode>'
        f'<faultstring>{faultstring}</faultstring>'
        f'</soap:Fault>'
    )


@pytest.fixture
def make_http() -> Callable[[SoapHandler], httpx.AsyncClient]:
    """Build an httpx.AsyncClient backed by a MockTransport that
    delegates to the test-supplied handler. The fixture returns a
    factory so each test can plug in its own handler closure."""

    def _factory(handler: SoapHandler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return _factory


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Reset the per-user DODP session map between tests so the
    plugin starts each test in a known state."""
    from openapis_ca_dodp import sessions
    sessions._clear_for_tests()
    yield
    sessions._clear_for_tests()


def _dodp_ns() -> str:
    from openapis_ca_dodp.client import DEFAULT_DODP_NS
    return DEFAULT_DODP_NS


def authenticated_handshake(handler: SoapHandler) -> SoapHandler:
    """Wrap a SOAP handler so it transparently answers the post-
    logOn handshake calls (getServiceAttributes +
    setReadingSystemAttributes). Lets per-test handlers focus on
    the operation under test instead of re-implementing the
    handshake boilerplate.
    """

    ns = _dodp_ns()

    def wrapper(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "getServiceAttributes" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<getServiceAttributesResponse xmlns="{ns}">'
                    f'  <serviceAttributes>'
                    f'    <service id="test-svc"><label><text>Test</text></label></service>'
                    f'    <supportsSearch>false</supportsSearch>'
                    f'  </serviceAttributes>'
                    f'</getServiceAttributesResponse>'
                ),
            )
        if "setReadingSystemAttributes" in body:
            return httpx.Response(
                200,
                text=soap_envelope(
                    f'<setReadingSystemAttributesResponse xmlns="{ns}">'
                    f'<setReadingSystemAttributesResult>true</setReadingSystemAttributesResult>'
                    f'</setReadingSystemAttributesResponse>'
                ),
            )
        return handler(request)

    return wrapper
