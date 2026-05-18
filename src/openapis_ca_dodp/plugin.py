"""openapis.ca-dodp -- Hummingbird plugin backed by a DODP server.

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

import logging
from pathlib import Path

import httpx

from hummingbird.models import BookRecord, FormatEntry, SearchResult
from hummingbird.plugins import Plugin

from . import sessions
from .client import DodpAuthFault, DodpClient, DodpFault
from .config import settings


logger = logging.getLogger("openapis_ca_dodp.plugin")


# Stand-in format-id Hummingbird shows in the bookshelf when the
# DODP server doesn't expose a per-format breakdown. NNELS uses
# 11000/11001/11500 for MP3 variants -- we pick something that
# doesn't collide so a mixed multi-plugin deployment would still
# be distinguishable in client UIs. (Plugins don't actually run
# side-by-side, but the convention is cheap to keep.)
DEFAULT_FORMAT_ID = 12000
DEFAULT_FORMAT_LABEL = "DAISY Online"


class OpenapisDodpPlugin(Plugin):
    """Speaks DODP to the configured base URL on behalf of the
    Hummingbird user. Each Hummingbird user gets one DODP session
    (cookie jar) shared across all hooks."""

    name = "openapis_dodp"

    def __init__(self, client: DodpClient | None = None) -> None:
        # Allow injection for tests; default to a singleton wired
        # to the env-config so plugin entry-point loaders that
        # call ``OpenapisDodpPlugin()`` with no args get a usable
        # instance.
        self._client = client or DodpClient(
            base_url=settings.base_url,
            namespace=settings.namespace,
            user_agent=settings.user_agent,
            timeout_seconds=settings.request_timeout_seconds,
        )

    # -- auth ---------------------------------------------------

    async def authenticate(self, username: str, password: str) -> bool:
        """Open (or reset) a session for ``username``: issue logOn
        against a fresh httpx client so the new credentials get a
        clean cookie jar, then stash the client for subsequent
        hooks to reuse."""
        await sessions.drop(username)
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
        sessions.put(username, sessions.UserSession(http=http))
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
                sess.http, settings.bookshelf_list_id,
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
        out: list[BookRecord] = []
        for item in items:
            node_id = sess.ids.to_int(item.content_id)
            out.append(
                BookRecord(
                    id=node_id,
                    title=item.label or item.content_id,
                    formats=[
                        FormatEntry(
                            id=DEFAULT_FORMAT_ID,
                            label=DEFAULT_FORMAT_LABEL,
                            narrator=None,
                        ),
                    ],
                ),
            )
        return out

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

    # -- search -------------------------------------------------

    async def search(
        self,
        username: str,
        query: str,
        formats: list[int] | None,
        page: int,
    ) -> SearchResult:
        # DODP v1 doesn't define search; v2 uses a structured
        # "questions" interaction that doesn't map onto
        # Hummingbird's flat list of BookRecords. Return an empty
        # result rather than raising NotImplementedError so the
        # REST endpoint just returns no matches instead of 500ing.
        return SearchResult(
            query=query, page=page, books=[],
            total_pages=0, total_results=0,
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
        try:
            resources = await self._client.get_content_resources(
                sess.http, dodp_id,
            )
        except DodpAuthFault:
            await sessions.drop(username)
            return None
        except DodpFault:
            return None
        if not resources:
            return None

        # Pick the first audio resource. DODP doesn't tag a
        # "primary" resource so we settle for the first
        # audio-shaped MIME -- callers wanting per-format
        # selection should query DODP directly. The cache dir
        # plus a stable filename derived from the contentID
        # avoids name collisions when the same user fetches
        # multiple books.
        audio = next(
            (r for r in resources if r.mime_type.startswith("audio/")),
            resources[0],
        )
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
