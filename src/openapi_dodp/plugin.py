"""openapi-dodp -- Hummingbird plugin backed by a DODP server.

Maps the Hummingbird 8-hook plugin contract onto DAISY Online
Delivery Protocol calls:

    authenticate          -> logOn
    list_bookshelf        -> getContentList(id="issued")
    add_to_bookshelf      -> issueContent
    remove_from_bookshelf -> returnContent
    search                -> not supported (DODP v1 has no search;
                             v2 expresses search as a question
                             flow which doesn't map onto the
                             plugin's flat-list contract). Returns
                             an empty SearchResult.
    set_bookmark          -> setBookmarks
    get_bookmark          -> getBookmarks
    download              -> getContentResources + stream the first
                             audio resource through the per-user
                             cookie jar so URLs gated behind the
                             session work

DODP contentIDs are strings; Hummingbird uses integer node_ids
across its plugin contract. We bridge with a per-user id map
(see ``sessions.IdMap``) that hands out small ints stable for
the life of one Hummingbird process.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from lxml import etree

from hummingbird.models import BookRecord, FormatEntry, SearchResult
from hummingbird.plugins import Plugin

from . import config, sessions
from .client import (
    Announcement,
    ContentItem,
    ContentResource,
    DodpAuthFault,
    DodpClient,
    DodpFault,
)


logger = logging.getLogger("openapi_dodp.plugin")


# DODP doesn't expose per-format BookRecord-friendly catalog at
# list_bookshelf time (the resources of a book are only knowable
# via a per-book getContentResources call, which would balloon
# list_bookshelf latency by N round-trips). We work around that
# by announcing a static set of format-ids per book and resolving
# the actual mime type at download time. The 12xxx range avoids
# collisions with NNELS' 11xxx range so a mixed deployment would
# still be distinguishable in client UIs.
#
# Each entry: format_id -> (mime_type, label). Operators can
# extend the announced set by overriding FORMAT_MAP at runtime
# (the BookRecord.formats list is built from ANNOUNCED_FORMATS,
# filtered to keys present in FORMAT_MAP).
FORMAT_MAP: dict[int, tuple[str, str]] = {
    12000: ("audio/mpeg", "MP3 audio"),
    12001: ("audio/mp4", "M4A audio"),
    12002: ("audio/wav", "WAV audio"),
    12003: ("application/zip", "DAISY ZIP"),
    12004: ("audio/ogg", "OGG audio"),
}

# What we announce per book by default. MP3 covers the vast
# majority of DAISY Online catalogs; ZIP covers structured
# DAISY books that ship as an archive. Operators wanting WAV
# or M4A entries can extend at deployment time.
ANNOUNCED_FORMATS: list[int] = [12000, 12003]


# Reading-system identification baked in here rather than via
# config because every hummingbird-with-this-plugin deployment
# IS a "hummingbird" reading system from the DODP server's
# point of view. Version is read lazily so a hummingbird
# upgrade doesn't require touching this plugin.
READING_SYSTEM_MANUFACTURER = "cobdfamily"
READING_SYSTEM_MODEL = "hummingbird"


def _hummingbird_version() -> str:
    try:
        from hummingbird import __version__ as v  # noqa: PLC0415

        return v
    except Exception:  # noqa: BLE001
        return "unknown"


READING_SYSTEM_VERSION = _hummingbird_version()


def _summarise_service_attrs(caps: dict) -> dict:
    """Pull the useful operator-visible fields out of the
    getServiceAttributes response. The element-to-dict shape
    varies wildly between DODP impls (some wrap fields in
    ``<serviceAttributes>``, some emit them flat; ``service``
    may be a string or a nested element); defensively normalise
    everything to ``None`` when the expected shape isn't there.

    We log this at INFO so a ``docker logs`` tail is readable;
    the full payload would drown out everything else.
    """
    inner = caps.get("serviceAttributes")
    if not isinstance(inner, dict):
        inner = caps if isinstance(caps, dict) else {}
    service = inner.get("service")
    if isinstance(service, dict):
        # Common shape: <service><label><text>X</text></label></service>
        label = service.get("label")
        if isinstance(label, dict):
            service_label = label.get("text") or label.get("@id")
        else:
            service_label = label
    else:
        service_label = service
    return {
        "service": service_label,
        "supportsSearch": inner.get("supportsSearch"),
        "supportsServerSideBack": inner.get("supportsServerSideBack"),
        "supportedOptionalOperations": inner.get(
            "supportedOptionalOperations",
        ),
    }


class OpenapiDodpPlugin(Plugin):
    """Speaks DODP to the configured base URL on behalf of the
    Hummingbird user. Each Hummingbird user gets one DODP session
    (cookie jar) shared across all hooks."""

    name = "openapi_dodp"

    def __init__(self, client: DodpClient | None = None) -> None:
        # Allow injection for tests; otherwise build a client
        # from env config. Missing base_url is a misconfig but
        # not a fatal one -- hummingbird's loader catches an
        # __init__ exception and silently falls back to
        # standalone, which is confusing to operators. Instead
        # we log a clear warning and leave self._client as None;
        # every hook then returns its "no session" fallback so
        # the service still serves a coherent (empty) bookshelf.
        if client is not None:
            self._client = client
        elif config.settings.base_url:
            s = config.settings
            self._client = DodpClient(
                base_url=s.base_url,
                namespace=s.namespace,
                user_agent=s.user_agent,
                timeout_seconds=s.request_timeout_seconds,
            )
        else:
            logger.warning(
                "OPENAPI_DODP_BASE_URL is not set -- plugin is "
                "loaded but every hook will be a no-op until "
                "the URL is configured and hummingbird restarts.",
            )
            self._client = None

    # -- auth ---------------------------------------------------

    async def logoff(self, username: str) -> None:
        """Issue DODP logOff for ``username``'s cached session, then
        drop the local session state. Best-effort: a fault from the
        server (eg. session already gone) is logged and swallowed
        -- the local drop still happens.

        Hummingbird doesn't yet have a logout plugin hook, but this
        method is called from ``authenticate`` when replacing an
        existing session (so the old DODP session is released
        cleanly upstream) and is exposed for future use.
        """
        if self._client is None:
            await sessions.drop(username)
            return
        sess = sessions.get(username)
        if sess is None:
            return
        try:
            await self._client.log_off(sess.http)
        except DodpFault as exc:
            logger.info(
                "logOff for %s faulted (continuing): %s",
                username, exc.faultstring,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "logOff for %s raised %s (continuing)",
                username, type(exc).__name__,
            )
        await sessions.drop(username)

    async def authenticate(self, username: str, password: str) -> bool:
        """Open (or reset) a session for ``username``: issue logOn
        against a fresh httpx client so the new credentials get a
        clean cookie jar, run the DODP service handshake
        (getServiceAttributes + setReadingSystemAttributes), then
        stash the client for subsequent hooks to reuse.

        getServiceAttributes failure is logged but not fatal --
        we log capability info for ops visibility and continue.
        setReadingSystemAttributes failure IS fatal because
        stricter DODP servers (KADOS at minimum) fault the next
        getContentList without it; we drop the session and
        return False so the user sees an auth failure rather
        than a silent "no books" empty bookshelf later.
        """
        if self._client is None:
            return False
        # Clean handoff: if an old session exists, issue a real
        # logOff against the server before forgetting it locally.
        # Otherwise the upstream session lingers (PHPSESSID
        # timeouts on KADOS run hours) wasting a slot.
        await self.logoff(username)
        http = httpx.AsyncClient(
            timeout=self._client.timeout_seconds,
            follow_redirects=True,
        )
        try:
            ok = await self._client.log_on(http, username, password)
        except DodpFault as exc:
            await http.aclose()
            logger.info(
                "logOn failed for %s: %s", username, exc.faultstring,
            )
            return False
        if not ok:
            # Server returned <logOnResult>false</logOnResult> with
            # no fault. Treat as auth failure (server's choice; we
            # don't second-guess).
            await http.aclose()
            return False

        # Handshake: capabilities first (logged for ops), then
        # reading-system identification.
        try:
            caps = await self._client.get_service_attributes(http)
            logger.info(
                "DODP service attributes for %s: %r",
                username, _summarise_service_attrs(caps),
            )
        except DodpFault as exc:
            # Servers that don't implement getServiceAttributes
            # will fault here; keep going since the spec lets
            # this be optional.
            logger.info(
                "getServiceAttributes failed for %s (continuing): %s",
                username, exc.faultstring,
            )

        try:
            await self._client.set_reading_system_attributes(
                http,
                manufacturer=READING_SYSTEM_MANUFACTURER,
                model=READING_SYSTEM_MODEL,
                version=READING_SYSTEM_VERSION,
            )
        except DodpFault as exc:
            # Hard failure: KADOS-style servers will then fault
            # every subsequent op. Drop the session so the user
            # sees the auth failure cleanly.
            logger.warning(
                "setReadingSystemAttributes failed for %s: %s",
                username, exc.faultstring,
            )
            await http.aclose()
            return False

        sessions.put(username, sessions.UserSession(http=http))

        # Surface any unread operator messages in the log so
        # ops can see them at `docker logs` time. Hummingbird
        # doesn't yet have a hook to relay announcements to the
        # end user; once it does, the plugin's public
        # service_announcements() method below is what the
        # hook should call.
        try:
            anns = await self._client.get_service_announcements(http)
            unread = [a for a in anns if not a.read]
            if unread:
                logger.info(
                    "DODP service has %d unread announcement(s) "
                    "for %s: %s",
                    len(unread), username,
                    [f"{a.id}:{a.text[:60]}" for a in unread],
                )
        except DodpFault as exc:
            # Spec lets servers omit announcements; if it faults
            # here we treat as "no announcements" and move on.
            logger.debug(
                "getServiceAnnouncements unavailable for %s: %s",
                username, exc.faultstring,
            )
        return True

    # -- bookshelf ---------------------------------------------

    async def list_bookshelf(self, username: str) -> list[BookRecord]:
        sess = sessions.get(username)
        if sess is None:
            # User passed Hummingbird's Basic-auth cache but their
            # DODP session was never established (or was dropped
            # after an auth fault). Surface an empty bookshelf
            # rather than crash; the next request that triggers
            # validate_credentials will re-auth and recover.
            logger.info(
                "list_bookshelf: no session for %s", username,
            )
            return []
        try:
            items = await self._client.get_content_list(
                sess.http, config.settings.bookshelf_list_id,
            )
        except DodpAuthFault:
            await sessions.drop(username)
            return []
        except DodpFault as exc:
            logger.warning(
                "getContentList failed for %s: %s",
                username, exc.faultstring,
            )
            return []

        # v0.8: fill the per-book resource cache for any books we
        # haven't probed yet. Parallel fan-out so a 30-book
        # bookshelf doesn't sequentially round-trip 30 times.
        await self._populate_resource_cache(username, sess, items)

        out: list[BookRecord] = []
        for item in items:
            node_id = sess.ids.to_int(item.content_id)
            out.append(
                BookRecord(
                    id=node_id,
                    title=item.label or item.content_id,
                    formats=_format_entries_for(
                        sess.resources.get(item.content_id),
                    ),
                ),
            )
        return out

    async def _populate_resource_cache(
        self,
        username: str,
        sess: sessions.UserSession,
        items: list[ContentItem],
    ) -> None:
        """Probe getContentResources for any content_ids not yet
        in sess.resources. Runs the probes in parallel; failures
        leave the cache entry absent so the fallback static-
        format-set kicks in for the affected book.
        """
        missing = [
            item.content_id
            for item in items
            if item.content_id not in sess.resources
        ]
        if not missing:
            return
        # Cap fan-out at a reasonable parallelism so a very
        # large bookshelf doesn't open hundreds of sockets at
        # once. asyncio.Semaphore would be ideal here but for
        # v0.8 we cap by issuing in chunks.
        chunk_size = 8
        for start in range(0, len(missing), chunk_size):
            chunk = missing[start : start + chunk_size]
            results = await asyncio.gather(
                *(
                    self._client.get_content_resources(sess.http, cid)
                    for cid in chunk
                ),
                return_exceptions=True,
            )
            for cid, result in zip(chunk, results, strict=True):
                if isinstance(result, DodpAuthFault):
                    # Session died upstream -- drop and bail.
                    await sessions.drop(username)
                    return
                if isinstance(result, BaseException):
                    logger.info(
                        "getContentResources(%s) for %s failed: %s",
                        cid, username, result,
                    )
                    continue
                sess.resources[cid] = result

    async def add_to_bookshelf(
        self, username: str, node_id: int,
    ) -> bool:
        return await self._issue_or_return(
            username, node_id, action="issueContent",
        )

    async def remove_from_bookshelf(
        self, username: str, node_id: int,
    ) -> bool:
        return await self._issue_or_return(
            username, node_id, action="returnContent",
        )

    async def _issue_or_return(
        self, username: str, node_id: int, *, action: str,
    ) -> bool:
        sess = sessions.get(username)
        if sess is None:
            return False
        dodp_id = sess.ids.to_dodp(node_id)
        if dodp_id is None:
            # Hummingbird is calling add/remove with a node_id we
            # never handed out -- means the user is operating on a
            # book they got from a list_bookshelf in a previous
            # process lifetime. Refresh the map first so they can
            # retry without restarting.
            await self.list_bookshelf(username)
            dodp_id = sess.ids.to_dodp(node_id)
            if dodp_id is None:
                return False
        try:
            if action == "issueContent":
                return await self._client.issue_content(sess.http, dodp_id)
            return await self._client.return_content(sess.http, dodp_id)
        except DodpAuthFault:
            await sessions.drop(username)
            return False
        except DodpFault as exc:
            logger.warning(
                "%s failed for %s/%s: %s",
                action, username, dodp_id, exc.faultstring,
            )
            return False

    # -- service announcements (v0.7) ---------------------------

    async def service_announcements(
        self, username: str,
    ) -> list[Announcement]:
        """Return the upstream's operator-message list for the
        user. Not part of the Hummingbird abstract Plugin contract
        -- exposed for future hummingbird endpoints that surface
        announcements to the DAISY player. Empty list on no-
        session / fault."""
        sess = sessions.get(username)
        if sess is None or self._client is None:
            return []
        try:
            return await self._client.get_service_announcements(sess.http)
        except DodpAuthFault:
            await sessions.drop(username)
            return []
        except DodpFault as exc:
            logger.warning(
                "getServiceAnnouncements failed for %s: %s",
                username, exc.faultstring,
            )
            return []

    async def mark_announcements_as_read(
        self, username: str, announcement_ids: list[str],
    ) -> bool:
        """Mark the listed announcements as read upstream so they
        don't resurface on the next login. False on no-session /
        fault."""
        sess = sessions.get(username)
        if sess is None or self._client is None:
            return False
        if not announcement_ids:
            return True
        try:
            return await self._client.mark_announcements_as_read(
                sess.http, announcement_ids,
            )
        except DodpAuthFault:
            await sessions.drop(username)
            return False
        except DodpFault as exc:
            logger.warning(
                "markAnnouncementsAsRead failed for %s: %s",
                username, exc.faultstring,
            )
            return False

    # -- search -------------------------------------------------

    async def search(
        self,
        username: str,
        query: str,
        formats: list[int] | None,
        page: int,
    ) -> SearchResult:
        """v0.9: best-effort DODP v2 search via getQuestions.

        DODP v1 has no search; v2 uses a recursive question /
        userResponse flow that's overkill for Hummingbird's
        flat (query, page) contract. We commit to handling at
        most one round-trip:

          1. getQuestions([]) -> discover the entry shape.
             If the server returns a contentList directly,
             use it.
          2. If it returned a single text question, send the
             user's query as a userResponse and getQuestions
             again. If THAT response is a contentList, use
             it. If it's another question, give up (multi-
             step search isn't representable in the flat
             contract).

        Servers that don't implement getQuestions (most v1
        impls) fault on step 1; we catch and return an empty
        result. The caller sees "no matches" instead of an
        error, matching the existing v0.x behaviour for the
        unimplemented case.
        """
        empty = SearchResult(
            query=query, page=page, books=[],
            total_pages=0, total_results=0,
        )
        sess = sessions.get(username)
        if sess is None or self._client is None:
            return empty
        try:
            step1 = await self._client.get_questions(sess.http, [])
        except DodpAuthFault:
            await sessions.drop(username)
            return empty
        except DodpFault as exc:
            logger.info(
                "search: getQuestions(empty) failed for %s: %s",
                username, exc.faultstring,
            )
            return empty

        items = _content_list_items(self._client.namespace, step1)
        if items is None:
            question_id = _first_input_question_id(
                self._client.namespace, step1,
            )
            if question_id is None:
                # No content list AND no input question we can
                # answer. Could be a multipleChoiceQuestion or
                # an unrecognised shape -- give up on search
                # rather than guess.
                return empty
            try:
                step2 = await self._client.get_questions(
                    sess.http,
                    [{"questionID": question_id, "value": query}],
                )
            except DodpAuthFault:
                await sessions.drop(username)
                return empty
            except DodpFault as exc:
                logger.info(
                    "search: getQuestions(response) failed for "
                    "%s: %s",
                    username, exc.faultstring,
                )
                return empty
            items = _content_list_items(self._client.namespace, step2)
            if items is None:
                # Multi-step flow we don't traverse.
                return empty

        # Map results to BookRecords + announce-only formats.
        # We could probe per-book resources like list_bookshelf
        # does but search results are usually a different set
        # than the bookshelf, and probing them all is wasteful
        # for the common "user scrolls past most results" case.
        # Use the static ANNOUNCED_FORMATS list here.
        format_entries = [
            FormatEntry(id=fid, label=FORMAT_MAP[fid][1], narrator=None)
            for fid in ANNOUNCED_FORMATS
            if fid in FORMAT_MAP
        ]
        records: list[BookRecord] = []
        for content_id, label in items:
            node_id = sess.ids.to_int(content_id)
            records.append(
                BookRecord(
                    id=node_id,
                    title=label or content_id,
                    formats=list(format_entries),
                ),
            )
        return SearchResult(
            query=query,
            page=page,
            books=records,
            total_pages=1,
            total_results=len(records),
        )

    # -- bookmarks ---------------------------------------------

    async def set_bookmark(
        self, username: str, content_id: int, bookmark: dict,
    ) -> bool:
        sess = sessions.get(username)
        if sess is None:
            return False
        dodp_id = sess.ids.to_dodp(content_id)
        if dodp_id is None:
            return False
        try:
            return await self._client.set_bookmarks(
                sess.http, dodp_id, bookmark,
            )
        except DodpAuthFault:
            await sessions.drop(username)
            return False
        except DodpFault:
            return False

    async def get_bookmark(
        self, username: str, content_id: int,
    ) -> dict:
        sess = sessions.get(username)
        if sess is None:
            return {}
        dodp_id = sess.ids.to_dodp(content_id)
        if dodp_id is None:
            return {}
        try:
            return await self._client.get_bookmarks(sess.http, dodp_id)
        except DodpAuthFault:
            await sessions.drop(username)
            return {}
        except DodpFault:
            return {}

    # -- download -----------------------------------------------

    async def download(
        self,
        username: str,
        fmt: int,
        node_id: int,
        cache_dir: Path,
    ) -> Path | None:
        sess = sessions.get(username)
        if sess is None:
            return None
        dodp_id = sess.ids.to_dodp(node_id)
        if dodp_id is None:
            return None
        # v0.8: hit the per-session cache first. List_bookshelf
        # populates it via parallel probes; download() only
        # falls back to a fresh SOAP call when the book wasn't
        # in the most recent bookshelf (eg. an orphan int that
        # the user persisted client-side).
        cached_resources = sess.resources.get(dodp_id)
        if cached_resources is not None:
            resources = cached_resources
        else:
            try:
                resources = await self._client.get_content_resources(
                    sess.http, dodp_id,
                )
            except DodpAuthFault:
                await sessions.drop(username)
                return None
            except DodpFault:
                return None
            sess.resources[dodp_id] = resources
        if not resources:
            return None

        # Pick the resource matching the requested fmt-id. Fall
        # back to "first audio-shaped MIME" only if the fmt is
        # unknown or no resource matches the requested mime --
        # this gives operators a way to extend FORMAT_MAP and
        # still have legacy clients (asking for the default fmt)
        # get something playable.
        audio = _select_resource_for_fmt(resources, fmt)
        if audio is None:
            logger.warning(
                "no resource matched fmt=%s for %s/%s; available: %s",
                fmt, username, dodp_id,
                [r.mime_type for r in resources],
            )
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = _suffix_for_mime(audio.mime_type) or ".bin"
        safe_id = dodp_id.replace("/", "_").replace("\\", "_")
        target = cache_dir / f"{safe_id}-{fmt}{suffix}"
        if target.exists() and target.stat().st_size > 0:
            return target
        try:
            async with sess.http.stream(
                "GET", audio.uri,
                timeout=self._client.timeout_seconds,
            ) as response:
                if response.status_code != 200:
                    logger.warning(
                        "download %s: HTTP %s",
                        audio.uri, response.status_code,
                    )
                    return None
                with target.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            logger.warning("download %s failed: %s", audio.uri, exc)
            if target.exists():
                target.unlink(missing_ok=True)
            return None
        return target


def _content_list_items(
    namespace: str, root: etree._Element,
) -> list[tuple[str, str]] | None:
    """Pull (content_id, label) pairs out of a DODP response
    that either contains a ``contentList`` (direct results) or
    a ``questions`` block (more input needed). Returns None
    when no contentList is present so the caller can branch on
    the question path instead."""
    found = root.find(f"{{{namespace}}}contentList")
    if found is None:
        # Some servers nest the contentList one level deeper.
        for desc in root.iter(f"{{{namespace}}}contentList"):
            found = desc
            break
    if found is None:
        return None
    items: list[tuple[str, str]] = []
    for item_el in found.iter(f"{{{namespace}}}contentItem"):
        cid = item_el.get("id") or ""
        label_el = item_el.find(f"{{{namespace}}}label")
        label = ""
        if label_el is not None:
            text_el = label_el.find(f"{{{namespace}}}text")
            label = (
                (text_el.text or "")
                if text_el is not None
                else (label_el.text or "")
            )
        if cid:
            items.append((cid, label.strip()))
    return items


def _first_input_question_id(
    namespace: str, root: etree._Element,
) -> str | None:
    """Pull the id of the first text-input question in a DODP
    getQuestions response. We support TEXT_NUMERIC + TEXT_
    ALPHANUMERIC; multiple-choice and other input types fall
    through to None (the search path then gives up).

    Different impls put the question element at different
    depths so we search the whole subtree."""
    for input_q in root.iter(f"{{{namespace}}}inputQuestion"):
        qid = input_q.get("id")
        if qid:
            return qid
    # Older v1 servers sometimes use a flat <question> element.
    for q in root.iter(f"{{{namespace}}}question"):
        qid = q.get("id")
        if qid:
            return qid
    return None


def _format_entries_for(
    resources: "list[ContentResource] | None",
) -> list[FormatEntry]:
    """Build the BookRecord.formats list for a book given the
    cached resource list. When the cache is absent (the resource
    probe failed) we fall back to ANNOUNCED_FORMATS so the user
    still sees something they can try -- download() will then
    drop back to its own fallback path. When the cache IS
    present, the formats list matches the upstream's actual
    catalog (deduped, in ANNOUNCED_FORMATS order so the UI sort
    is stable across books)."""
    if resources is None:
        return [
            FormatEntry(id=fid, label=FORMAT_MAP[fid][1], narrator=None)
            for fid in ANNOUNCED_FORMATS
            if fid in FORMAT_MAP
        ]
    available_mimes = {r.mime_type.lower() for r in resources}
    matching: list[FormatEntry] = []
    for fid in ANNOUNCED_FORMATS:
        entry = FORMAT_MAP.get(fid)
        if entry is None:
            continue
        mime, label = entry
        if mime.lower() in available_mimes:
            matching.append(
                FormatEntry(id=fid, label=label, narrator=None),
            )
    # If the catalog has audio-shaped resources that aren't in
    # ANNOUNCED_FORMATS, still surface the default fmt so the
    # client UI shows a playable option rather than an empty
    # format list.
    if not matching:
        for r in resources:
            if r.mime_type.lower().startswith("audio/"):
                default_fid = ANNOUNCED_FORMATS[0] if ANNOUNCED_FORMATS else 12000
                if default_fid in FORMAT_MAP:
                    matching.append(
                        FormatEntry(
                            id=default_fid,
                            label=FORMAT_MAP[default_fid][1],
                            narrator=None,
                        ),
                    )
                break
    return matching


def _select_resource_for_fmt(
    resources: "list[ContentResource]", fmt: int,
) -> "ContentResource | None":
    """Pick the resource that matches the requested format-id.

    Lookup order:

      1. Exact mime match for the requested fmt (case-
         insensitive). This is the right answer for clients
         that called list_bookshelf, saw FormatEntry(id=12000),
         and asked for fmt=12000.

      2. If the fmt isn't in our table, or no resource matches
         its mime, fall back to the first audio-shaped
         resource. Better to serve SOMETHING playable than to
         404 a client that asked for a fmt the upstream
         doesn't carry.

      3. If there are no audio resources at all, return None
         and let the caller report the failure.
    """
    target_mime = FORMAT_MAP.get(fmt, (None, None))[0]
    if target_mime is not None:
        target_lower = target_mime.lower()
        for r in resources:
            if r.mime_type.lower() == target_lower:
                return r
    # Fallback: first audio resource regardless of fmt.
    for r in resources:
        if r.mime_type.lower().startswith("audio/"):
            return r
    return None


def _suffix_for_mime(mime_type: str) -> str | None:
    """Best-effort filename suffix from a content type. DODP
    servers commonly ship audio/mpeg + audio/mp4 + audio/wav;
    we cover those plus a couple common DAISY archive types."""
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "application/zip": ".zip",
        "application/epub+zip": ".epub",
    }
    return mapping.get(mime_type.lower())
