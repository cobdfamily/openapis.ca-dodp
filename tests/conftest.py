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
