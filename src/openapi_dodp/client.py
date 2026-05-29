"""Minimal DAISY Online Delivery Protocol (DODP) SOAP client.

The protocol is a thin SOAP/HTTP layer with a fixed envelope shape:
the body holds one element named after the method, in the DODP
namespace; each child is a parameter, also in the DODP namespace.
Responses mirror it (``<method>Response`` with a ``<method>Result``
child for simple types, or an inline complex element).

This client implements just the operations Hummingbird needs:

    logOn / logOff
    getContentList (the "bookshelf")
    getContentMetadata
    getContentResources
    issueContent / returnContent
    setBookmarks / getBookmarks

We deliberately don't use ``zeep`` because:
  - DODP impls in the wild ship slightly-non-conformant WSDLs
    (Kolibre KADOS is one) that zeep rejects on parse;
  - the envelope set above is small enough that hand-building
    the XML keeps the dep footprint tiny;
  - we never need WSDL-driven complex types -- responses are
    parsed by element name + namespace, not by xsd:type.

Faults raise ``DodpFault``. Auth failures (the protocol returns
a Fault on bad credentials, not a 401) become
``DodpAuthFault`` so callers can distinguish "session went away"
from "library returned an error".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from lxml import etree

logger = logging.getLogger(__name__)


# DODP v1 + v2 share the same namespace string; the version
# negotiation lives in getServiceAttributes payload bodies. Some
# DAISY-Consortium pre-release servers used "/v2/" suffix --
# operators override via OPENAPI_DODP_NAMESPACE.
DEFAULT_DODP_NS = "http://www.daisy.org/ns/daisy-online/"

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_XML_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'


# Hardened parser for upstream SOAP responses. The defaults on
# ``etree.fromstring`` accept DTDs and (depending on libxml2 version)
# resolve external entities -- billion-laughs DoS and historical XXE
# CVEs both apply. We don't need either for DODP: the protocol uses
# no DTDs and no entities. Lock both off plus ``no_network`` (don't
# fetch external DTDs even if one is declared) and ``huge_tree``
# (already off by default but explicit > implicit for security).
_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
    recover=False,
)


def _parse_xml(content: bytes) -> etree._Element:
    """Parse upstream SOAP with the hardened parser. Centralized so the
    two call sites in ``_call`` can't drift apart on settings."""
    return etree.fromstring(content, _PARSER)


class DodpFault(RuntimeError):
    """Generic SOAP fault from the DODP server. ``faultstring`` is
    the human-readable message; ``faultcode`` is the SOAP code
    (Server / Client / etc)."""

    def __init__(self, faultstring: str, faultcode: str = "") -> None:
        super().__init__(faultstring)
        self.faultstring = faultstring
        self.faultcode = faultcode


class DodpAuthFault(DodpFault):
    """The fault matched an auth-shaped condition: invalid
    credentials, session expired, or the operation requires
    logOn. Callers can drop the cached session and force a
    re-auth."""


@dataclass(slots=True)
class ContentItem:
    """Subset of a DODP ``contentItem`` element we surface upward.
    ``label`` is the human-readable title; ``last_modified_date``
    is left as the raw protocol string (the field is rarely used
    by Hummingbird callers, and parsing it would require dateutil
    just to handle the protocol's optional TZ form)."""

    content_id: str
    label: str
    last_modified_date: str | None = None


@dataclass(slots=True)
class ContentResource:
    """One file in a DODP ``resources`` payload."""

    uri: str
    mime_type: str
    size_bytes: int | None
    local_uri: str | None


@dataclass(slots=True)
class Announcement:
    """A DODP service announcement: an operator message
    targeted at a user. DAISY clients display the text; the
    ``read`` flag persists upstream so a user doesn't see
    the same announcement on every login."""

    id: str
    text: str
    type: str | None = None
    priority: str | None = None
    read: bool = False


class DodpClient:
    """Stateless DODP SOAP client. Stateless meaning the client
    holds no per-request state -- the caller supplies an
    ``httpx.AsyncClient`` whose cookie jar holds the DODP session
    cookie. This pattern lets one client instance serve many
    concurrent users (each with their own AsyncClient + cookies)
    without locking."""

    def __init__(
        self,
        base_url: str,
        *,
        namespace: str = DEFAULT_DODP_NS,
        user_agent: str = "openapi-dodp/0.1",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("DODP base_url is required")
        self.base_url = base_url
        self.namespace = namespace
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds

    # -- public API ---------------------------------------------

    async def log_on(
        self, http: httpx.AsyncClient, username: str, password: str,
    ) -> bool:
        """DODP logOn. Server sets a session cookie on the response
        which httpx's cookie jar keeps for subsequent calls on the
        same client.

        Bad credentials surface as a DodpAuthFault (the SOAP
        fault matches the auth-keyword heuristic). Callers that
        want a plain bool should catch DodpFault and treat it as
        failure -- that's what the plugin's authenticate hook
        does.
        """
        root = await self._call(
            http, "logOn",
            {"username": username, "password": password},
        )
        return self._result_bool(root, "logOn")

    async def log_off(self, http: httpx.AsyncClient) -> bool:
        root = await self._call(http, "logOff", {})
        return self._result_bool(root, "logOff")

    async def get_service_attributes(
        self, http: httpx.AsyncClient,
    ) -> dict[str, Any]:
        """Capability negotiation. Spec says this MUST be called
        after logOn before other operations; in practice most
        servers tolerate skipping it, but stricter impls will
        fault on the next call. We send it as part of the post-
        logOn handshake and surface the response so callers can
        log the upstream's capability set."""
        root = await self._call(http, "getServiceAttributes", {})
        return self._element_to_dict(root)

    async def set_reading_system_attributes(
        self,
        http: httpx.AsyncClient,
        *,
        manufacturer: str,
        model: str,
        version: str,
        serial_number: str | None = None,
        supported_content_formats: list[str] | None = None,
        supported_mime_types: list[str] | None = None,
        supported_input_types: list[str] | None = None,
        requires_audio_labels: bool = False,
        preferred_ui_language: str = "en",
    ) -> bool:
        """Identify the reading system to the server. Many DAISY
        servers (KADOS included) require this immediately after
        logOn -- without it, getContentList faults with "client
        not initialised". We default to sensible values that
        describe a generic hummingbird-fronted reading system
        and let callers override.

        The ``config`` sub-element wraps the per-capability
        switches. Lists become repeated children of their parent
        element (eg. several ``<mimeType>`` children inside
        ``<supportedMimeTypes>``).
        """
        # Per the DODP schema, supportedMimeTypes wraps
        # <mimeType>X</mimeType> children, not <supportedMimeTypes>
        # children. The _append_param helper renders a list as
        # repeated parent elements which is the wrong shape, so
        # we build the bookkeeping dict here with the correct
        # singular child tags.
        config: dict[str, Any] = {
            "supportedContentFormats": {
                "contentFormat": supported_content_formats
                or ["ANSI/NISO Z39.86-2005", "DAISY 2.02"]
            },
            "supportedMimeTypes": {
                "mimeType": supported_mime_types
                or ["audio/mpeg", "audio/mp4", "application/zip"]
            },
            "supportedInputTypes": {
                "input": supported_input_types
                or ["TEXT_NUMERIC", "TEXT_ALPHANUMERIC"]
            },
            "requiresAudioLabels": "true" if requires_audio_labels else "false",
            "preferredUILanguage": preferred_ui_language,
        }
        attrs: dict[str, Any] = {
            "manufacturer": manufacturer,
            "model": model,
            "serialNumber": serial_number or "",
            "version": version,
            "config": config,
        }
        root = await self._call(
            http,
            "setReadingSystemAttributes",
            {"readingSystemAttributes": attrs},
        )
        return self._result_bool(root, "setReadingSystemAttributes")

    async def get_content_list(
        self, http: httpx.AsyncClient, list_id: str,
    ) -> list[ContentItem]:
        root = await self._call(http, "getContentList", {"id": list_id})
        # Some servers nest the contentList in a Response wrapper;
        # others put contentItem children directly under the
        # response element. Walk descendants by local-name to
        # tolerate both shapes.
        items: list[ContentItem] = []
        for item_el in root.iter(f"{{{self.namespace}}}contentItem"):
            cid = item_el.get("id") or ""
            label = ""
            label_el = item_el.find(f"{{{self.namespace}}}label")
            if label_el is not None:
                # <label> may wrap <text>...</text> (DODP v2) or
                # contain the text directly (some v1 impls).
                text_el = label_el.find(f"{{{self.namespace}}}text")
                label = (
                    (text_el.text or "")
                    if text_el is not None
                    else (label_el.text or "")
                )
            last_mod = item_el.findtext(
                f"{{{self.namespace}}}lastModifiedDate"
            )
            items.append(
                ContentItem(
                    content_id=cid,
                    label=label.strip(),
                    last_modified_date=last_mod,
                )
            )
        return items

    async def get_content_metadata(
        self, http: httpx.AsyncClient, content_id: str,
    ) -> dict[str, Any]:
        root = await self._call(
            http, "getContentMetadata", {"contentID": content_id},
        )
        return self._element_to_dict(root)

    async def get_content_resources(
        self, http: httpx.AsyncClient, content_id: str,
    ) -> list[ContentResource]:
        root = await self._call(
            http, "getContentResources", {"contentID": content_id},
        )
        out: list[ContentResource] = []
        for res in root.iter(f"{{{self.namespace}}}resource"):
            uri = res.get("uri") or ""
            mime_type = res.get("mimeType") or ""
            size_str = res.get("size")
            local_uri = res.get("localURI")
            try:
                size = int(size_str) if size_str else None
            except ValueError:
                size = None
            out.append(
                ContentResource(
                    uri=uri,
                    mime_type=mime_type,
                    size_bytes=size,
                    local_uri=local_uri,
                )
            )
        return out

    async def issue_content(
        self, http: httpx.AsyncClient, content_id: str,
    ) -> bool:
        root = await self._call(
            http, "issueContent", {"contentID": content_id},
        )
        return self._result_bool(root, "issueContent")

    async def return_content(
        self, http: httpx.AsyncClient, content_id: str,
    ) -> bool:
        root = await self._call(
            http, "returnContent", {"contentID": content_id},
        )
        return self._result_bool(root, "returnContent")

    async def get_bookmarks(
        self, http: httpx.AsyncClient, content_id: str,
    ) -> dict[str, Any]:
        root = await self._call(
            http, "getBookmarks", {"contentID": content_id},
        )
        return self._element_to_dict(root)

    async def get_questions(
        self,
        http: httpx.AsyncClient,
        user_responses: list[dict[str, str]] | None = None,
    ) -> etree._Element:
        """Drive the DODP v2 question/answer search flow.

        On the first call (``user_responses`` empty or None) the
        server returns either an entry question OR a
        contentList directly. The caller picks the right path:
        if it's a contentList, parse + return; if a question,
        build a userResponse with the user's input and call
        again.

        Returns the parsed response element so callers can
        inspect both shapes. The DODP v2 question protocol is
        recursive in principle; this client commits to handling
        at most one round-trip beyond the initial probe (v1's
        scope is "simple text search"; multi-step / faceted
        flows are caller responsibility).
        """
        params: dict[str, Any] = {}
        if user_responses:
            # Map [{"questionID": "x", "value": "y"}, ...] to
            # the DODP wire shape: <userResponses> wrapping one
            # <userResponse questionID=... value=.../> per
            # entry. The @-prefixed dict keys land as XML
            # attributes via _append_param.
            params["userResponses"] = {
                "userResponse": [
                    {"@questionID": r["questionID"], "@value": r["value"]}
                    for r in user_responses
                ],
            }
        return await self._call(http, "getQuestions", params)

    async def get_service_announcements(
        self, http: httpx.AsyncClient,
    ) -> list[Announcement]:
        """List operator messages targeted at the logged-in user.
        Returns an empty list if the server has no announcements
        or doesn't implement the operation (faults bubble up as
        DodpFault for the caller to log + ignore)."""
        root = await self._call(http, "getServiceAnnouncements", {})
        out: list[Announcement] = []
        for ann in root.iter(f"{{{self.namespace}}}announcement"):
            text_el = ann.find(f"{{{self.namespace}}}text")
            out.append(
                Announcement(
                    id=ann.get("id") or "",
                    text=(text_el.text if text_el is not None else "") or "",
                    type=ann.get("type"),
                    priority=ann.get("priority"),
                    read=(ann.get("read") or "").lower() == "true",
                )
            )
        return out

    async def mark_announcements_as_read(
        self, http: httpx.AsyncClient, announcement_ids: list[str],
    ) -> bool:
        """Mark one or more announcements as read upstream so they
        don't resurface on the next login. The DODP body shape
        wraps each id in an ``<item>`` element inside a ``<read>``
        wrapper -- _append_param's list-repeat behaviour handles
        the ``<item>`` repetition by itself.
        """
        if not announcement_ids:
            return True
        root = await self._call(
            http,
            "markAnnouncementsAsRead",
            {"read": {"item": list(announcement_ids)}},
        )
        return self._result_bool(root, "markAnnouncementsAsRead")

    async def set_bookmarks(
        self,
        http: httpx.AsyncClient,
        content_id: str,
        bookmark_set: dict[str, Any],
    ) -> bool:
        # The DODP bookmarkSet is a structured element with
        # nested positions / playback rates / etc. We accept a
        # plain dict from the caller and let lxml emit it
        # recursively; nested dicts become nested elements.
        root = await self._call(
            http,
            "setBookmarks",
            {
                "contentID": content_id,
                "bookmarkSet": bookmark_set,
            },
        )
        return self._result_bool(root, "setBookmarks")

    # -- internals ----------------------------------------------

    async def _call(
        self,
        http: httpx.AsyncClient,
        method: str,
        params: dict[str, Any],
        *,
        auth_check: bool = True,
    ) -> etree._Element:
        envelope = self._build_envelope(method, params)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"/{method}"',
            "User-Agent": self.user_agent,
        }
        try:
            response = await http.post(
                self.base_url,
                content=envelope,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DodpFault(
                f"transport error calling {method}: {exc}",
                faultcode="Transport",
            ) from exc

        if response.status_code >= 500:
            # Some impls still return a SOAP envelope on 500 with
            # a Fault inside; try parsing first.
            try:
                root = _parse_xml(response.content)
            except etree.XMLSyntaxError:
                raise DodpFault(
                    f"HTTP {response.status_code} from {method}: "
                    f"{response.text[:200]}",
                    faultcode="Server",
                ) from None
            self._raise_for_fault(root, method, auth_check=auth_check)
            # Fault-less 5xx with XML body: surface the status.
            raise DodpFault(
                f"HTTP {response.status_code} from {method}",
                faultcode="Server",
            )
        if response.status_code != 200:
            raise DodpFault(
                f"HTTP {response.status_code} from {method}: "
                f"{response.text[:200]}",
                faultcode=str(response.status_code),
            )

        try:
            root = _parse_xml(response.content)
        except etree.XMLSyntaxError as exc:
            raise DodpFault(
                f"malformed XML from {method}: {exc}",
                faultcode="Client",
            ) from exc

        self._raise_for_fault(root, method, auth_check=auth_check)
        return self._unwrap_body(root, method)

    def _build_envelope(
        self, method: str, params: dict[str, Any],
    ) -> bytes:
        nsmap = {None: self.namespace, "soap": SOAP_NS}
        envelope = etree.Element(f"{{{SOAP_NS}}}Envelope", nsmap=nsmap)
        body = etree.SubElement(envelope, f"{{{SOAP_NS}}}Body")
        method_el = etree.SubElement(
            body, f"{{{self.namespace}}}{method}",
        )
        for key, value in params.items():
            self._append_param(method_el, key, value)
        return _XML_DECL + etree.tostring(envelope, encoding="utf-8")

    def _append_param(
        self, parent: etree._Element, name: str, value: Any,
    ) -> None:
        """Recursively render a parameter. Nested dicts become
        nested elements (used by ``bookmarkSet``); lists become
        repeated sibling elements with the same tag. Keys
        starting with ``@`` become XML attributes on the parent
        element rather than child elements -- needed for the
        DODP v2 ``<userResponse questionID="..." value="..."/>``
        shape (the spec puts those on the element rather than
        in child tags).
        """
        if isinstance(value, dict):
            child = etree.SubElement(parent, f"{{{self.namespace}}}{name}")
            for sub_name, sub_value in value.items():
                if sub_name.startswith("@"):
                    child.set(sub_name[1:], str(sub_value))
                else:
                    self._append_param(child, sub_name, sub_value)
            return
        if isinstance(value, list):
            for entry in value:
                self._append_param(parent, name, entry)
            return
        child = etree.SubElement(parent, f"{{{self.namespace}}}{name}")
        child.text = "" if value is None else str(value)

    def _unwrap_body(
        self, root: etree._Element, method: str,
    ) -> etree._Element:
        body = root.find(f"{{{SOAP_NS}}}Body")
        if body is None:
            raise DodpFault(
                f"no soap:Body in response to {method}",
                faultcode="Client",
            )
        # Preferred: <method>Response in the DODP namespace.
        resp = body.find(f"{{{self.namespace}}}{method}Response")
        if resp is not None:
            return resp
        # Fall back to the first child of body (some non-conformant
        # servers omit the Response wrapper). Log a warning so operators
        # see protocol drift -- combined with _result_bool's strict
        # "missing result element = False" rule below, this prevents
        # the historic fail-open path where a server returning
        # <soap:Body><randomElement/></soap:Body> made log_on() return
        # True for any 200 response without a Fault.
        first_child = next(iter(body), None)
        if first_child is None:
            raise DodpFault(
                f"empty soap:Body in response to {method}",
                faultcode="Client",
            )
        logger.warning(
            "DODP %s response missing expected %sResponse wrapper; "
            "fell back to first body child %s. This may indicate a "
            "non-conformant upstream or a protocol drift.",
            method, method, etree.QName(first_child).localname,
        )
        return first_child

    def _raise_for_fault(
        self,
        root: etree._Element,
        method: str,
        *,
        auth_check: bool,
    ) -> None:
        body = root.find(f"{{{SOAP_NS}}}Body")
        if body is None:
            return
        fault = body.find(f"{{{SOAP_NS}}}Fault")
        if fault is None:
            return
        faultstring = (fault.findtext("faultstring") or "").strip()
        faultcode = (fault.findtext("faultcode") or "").strip()
        # Heuristic for auth: DODP servers vary widely on fault
        # text but invariably mention one of these terms when the
        # session is wrong. Bare minimum: catch the most common
        # KADOS phrasing ("session not initialised", "not logged
        # in", "invalid credentials"). Operators can extend the
        # heuristic if their backend produces something exotic.
        lowered = faultstring.lower()
        if auth_check and any(
            kw in lowered
            for kw in (
                "not logged",
                "session",
                "invalid credential",
                "authentication",
                "not authorized",
                "unauthorized",
            )
        ):
            raise DodpAuthFault(faultstring or method, faultcode)
        raise DodpFault(
            faultstring or f"unspecified fault from {method}",
            faultcode,
        )

    def _result_bool(self, root: etree._Element, method: str) -> bool:
        """Most boolean-returning DODP methods wrap the result in
        ``<methodResult>true|false</methodResult>``. Return False on
        missing or non-truthy values: fail-closed on missing wrapper
        prevents the historic ``log_on`` fail-open where a server
        returning ``<soap:Body><randomElement/></soap:Body>`` combined
        with ``_unwrap_body``'s first-child fallback would silently
        authenticate. For mutating/auth methods, "no explicit result"
        is unsafe to treat as success."""
        result_el = root.find(f"{{{self.namespace}}}{method}Result")
        if result_el is None:
            # Try the iter() fallback for impls that nest the result
            # under an extra <return> wrapper or similar. Still strict:
            # only the namespaced exact-name match counts.
            result_el = next(
                root.iter(f"{{{self.namespace}}}{method}Result"), None,
            )
        if result_el is None:
            logger.warning(
                "DODP %s response had no <%sResult> element; treating "
                "as False (fail-closed).",
                method, method,
            )
            return False
        return (result_el.text or "").strip().lower() == "true"

    def _element_to_dict(self, element: etree._Element) -> dict[str, Any]:
        """Convert a DODP element subtree into a dict suitable for
        Hummingbird's plugin contract. Attributes carry over as
        ``"@attr"`` keys; element text becomes the ``"text"`` key
        when an element also has children. Pure-text leaves become
        plain strings."""
        out: dict[str, Any] = {}
        for attr_name, attr_value in element.attrib.items():
            out[f"@{attr_name}"] = attr_value
        for child in element:
            tag = etree.QName(child).localname
            value: Any
            if len(child) == 0 and not child.attrib:
                value = (child.text or "").strip()
            else:
                value = self._element_to_dict(child)
            # Multiple children with the same tag become a list.
            if tag in out:
                existing = out[tag]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    out[tag] = [existing, value]
            else:
                out[tag] = value
        if element.text and element.text.strip() and not out:
            return {"text": element.text.strip()}
        return out
