"""Per-user DODP session storage + DODP-id <-> hummingbird-int map.

DODP's contentID is a string ("con-12345"); Hummingbird's plugin
contract uses ``int`` node_ids. We can't safely round-trip the
DODP shape through ``int()`` so we keep a per-user bidirectional
map: at most one stable int per DODP contentID seen during a
session.

Session state is process-local. A Hummingbird restart drops it;
clients re-authenticate via Basic auth and pick a new mapping.
That's acceptable for v1 -- the int side of the map is opaque
to clients (they just hand back whatever Hummingbird previously
returned).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx


if TYPE_CHECKING:  # pragma: no cover - import-time only
    pass


@dataclass(slots=True)
class IdMap:
    """Bidirectional contentID <-> small-int map. Caller-supplied
    DODP IDs may be anything (UUIDs, numeric strings, slug paths)
    so we never assume a numeric form. The int side is an
    incrementing counter we control."""

    _next: int = 1
    _dodp_to_int: dict[str, int] = field(default_factory=dict)
    _int_to_dodp: dict[int, str] = field(default_factory=dict)

    def to_int(self, dodp_id: str) -> int:
        if dodp_id in self._dodp_to_int:
            return self._dodp_to_int[dodp_id]
        n = self._next
        self._next += 1
        self._dodp_to_int[dodp_id] = n
        self._int_to_dodp[n] = dodp_id
        return n

    def to_dodp(self, node_id: int) -> str | None:
        return self._int_to_dodp.get(node_id)


@dataclass(slots=True)
class UserSession:
    """One DODP session per Hummingbird user: an httpx client with
    the server's session cookie + the id map. ``http`` is
    constructed lazily by the plugin so this dataclass can be
    cheap to instantiate at registration time."""

    http: httpx.AsyncClient
    ids: IdMap = field(default_factory=IdMap)


_sessions: dict[str, UserSession] = {}


def get(username: str) -> UserSession | None:
    return _sessions.get(username)


def put(username: str, session: UserSession) -> None:
    _sessions[username] = session


async def drop(username: str) -> None:
    """Forget a session and close its underlying HTTP client.
    Called on logoff or after an auth-fault upstream so the next
    request starts a fresh httpx client (and re-issues logOn)."""
    sess = _sessions.pop(username, None)
    if sess is not None:
        try:
            await sess.http.aclose()
        except Exception:  # noqa: BLE001
            pass


def _clear_for_tests() -> None:
    """Test-only: wipe all sessions. Production code never calls
    this; the dict is process-local and lives until restart."""
    _sessions.clear()
