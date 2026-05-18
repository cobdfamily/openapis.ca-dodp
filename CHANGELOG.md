# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: SemVer; pre-1.0 minor bumps may break.

## [Unreleased]

## [0.7.0] - 2026-05-18

### Added

- **DODP service announcements** (`getServiceAnnouncements`
  + `markAnnouncementsAsRead`). Operator messages targeted at
  a specific user (eg. "library closing Friday"). DAISY
  players render them above the bookshelf.

  Surfaced two ways:

  - **At authenticate time**, the plugin now polls
    `getServiceAnnouncements` and logs any unread entries at
    INFO. Operators see them in `docker logs` without
    needing a new endpoint. Fault here is non-fatal; the
    spec allows servers to omit the operation.

  - **Plugin methods**
    `service_announcements(username)` and
    `mark_announcements_as_read(username, ids)`. NOT part of
    the Hummingbird abstract Plugin contract (the upstream
    hasn't shipped the hook yet); exposed for the future
    `/v1/announcements` REST endpoint to call when it lands.

### Tests

3 new (lists_unread, mark_as_read wire shape, empty-list
no-op short-circuit). 39 total. Conftest's
`authenticated_handshake` wrapper now answers
getServiceAnnouncements alongside the other post-logOn
calls; the wrapper also flipped to "inner handler wins"
ordering so tests can opt-in to their own announcement
responses without restating the whole handshake.

## [0.6.0] - 2026-05-18

### Added

- **`.github/workflows/release.yml`** -- builds and pushes
  the multi-arch (amd64 + arm64) container image to kibble
  on every `git tag v*`. Tagged twice: with the version
  and as `latest`. Matches the nnels release shape.
- **`logoff(username)` plugin method** -- issues DODP
  `logOff` against the user's cached session, then drops
  the local state. Best-effort: server-side faults (eg.
  "session already gone") are logged and swallowed so a
  stale local state can't trap a user out of re-auth.
- **Clean session handoff on re-authentication.** When a
  user re-authenticates (token rotation, password reset),
  the plugin now issues `logOff` against the upstream
  before forgetting the local session. Previously the
  old DODP session lingered on the server for the
  upstream's session-timeout (hours on KADOS).

### Tests

2 new (logOff on auth replacement, fault-tolerant
local-drop). 36 total.

## [0.5.0] - 2026-05-18

### Added

- **`CHANGELOG.md`** (retroactive entries for v0.1-v0.4).
- **`DEPLOYMENT.md`** — full operator checklist mirroring the
  cobdfamily/hummingbird + cobdfamily/nnels DEPLOYMENT.md
  shape (image distribution via kibble, configure / run /
  verify, upgrade flow, common failure modes).

## [0.4.0] - 2026-05-18

### Added

- **GitHub Actions test workflow** (`.github/workflows/
  test.yml`). Three jobs: ruff lint, pytest with coverage
  gate, end-to-end Dockerfile build. Nightly schedule at
  07:00 UTC catches hummingbird-base regressions within
  24h. Sibling-checks out cobdfamily/hummingbird because
  the `[tool.uv.sources]` block in `pyproject.toml`
  resolves it from `../hummingbird`.
- **`[tool.coverage]` config** scoping reports to the
  `openapis_ca_dodp` package + a `fail_under = 78` floor.
  78% is the v0.x baseline; expected to climb as auth-
  fault-recovery branches gain per-hook coverage.

## [0.3.0] - 2026-05-18

### Added

- **Per-format download.** `list_bookshelf` now announces
  multiple `FormatEntry` per book (defaults: MP3 + DAISY
  ZIP); `download()` honours the requested `fmt` parameter
  by mime-matching against `getContentResources`. A
  module-level `FORMAT_MAP` maps fmt-id → (mime, label) for
  five common formats (MP3, M4A, WAV, ZIP, OGG); operators
  extend by mutating it at deployment.
- **Fallback resource selection.** When the requested fmt
  isn't in the catalog, `download()` falls back to "first
  audio-shaped resource" + logs the mismatch for ops
  visibility, rather than 404'ing the client.

### Tests

3 new (multi-format announce, fmt-driven resource pick,
graceful fallback). 34 total.

## [0.2.0] - 2026-05-18

### Added

- **Full DODP handshake after `logOn`.** v0.1 stopped after
  `logOn`, which works against lenient servers but Kolibre
  KADOS faults the next `getContentList` with "client not
  initialised". The plugin now calls:

  - `getServiceAttributes` — best-effort. Logged for ops at
    INFO; faults are tolerated (the spec allows skipping).
  - `setReadingSystemAttributes` — load-bearing. Identifies
    the reading system as `cobdfamily/hummingbird` with the
    live hummingbird version. A fault here drops the
    session and returns 401 so the user sees the failure
    cleanly instead of a silent empty bookshelf later.

### Tests

5 new (handshake call order, getServiceAttributes-tolerant,
setReadingSystemAttributes hard-fail, wire shape of
`<supportedMimeTypes><mimeType>` singular-child tag). 31
total.

## [0.1.2] - 2026-05-18

### Added

- **Dockerfile** — cobdfamily/hummingbird base + this plugin
  pip-installed on top + `HUMMINGBIRD_PLUGIN=openapis_dodp`
  default. No native deps (pure-wheel httpx + lxml +
  pydantic-settings).
- **docker-compose.yaml** — references the kibble-published
  image; exposes the operator-mandatory
  `OPENAPIS_DODP_BASE_URL` env var via a shell variable so a
  missing value is obvious at `docker compose config` time.

## [0.1.1] - 2026-05-18

### Fixed

- **Graceful degrade when `OPENAPIS_DODP_BASE_URL` is unset.**
  v0.1.0 raised `ValueError` from `__init__`, which
  hummingbird's loader caught and silently dropped the plugin.
  Operators saw a stacktrace + no clear "plugin disabled"
  signal. Now the plugin loads with `self._client = None`,
  logs a clear warning at boot, and every hook returns its
  existing empty-session fallback.
- **Setting-singleton refresh.** Switched `plugin.py` from
  `from .config import settings` (bind-at-import) to
  `from . import config` + `config.settings.X` lookups so
  test-time settings rebuilds are visible to the plugin.

### Tests

3 new integration tests (end-to-end Hummingbird boot + REST
through this plugin against a mocked DODP server). 26 total.

## [0.1.0] - 2026-05-18

### Added

Initial scaffold. Hummingbird plugin proxying to a DAISY
Online Delivery Protocol (DODP) backend. Maps the eight
Hummingbird plugin hooks onto DODP SOAP operations:

  | Hummingbird          | DODP                        |
  | -------------------- | --------------------------- |
  | authenticate         | logOn                       |
  | list_bookshelf       | getContentList(id="issued") |
  | add_to_bookshelf     | issueContent                |
  | remove_from_bookshelf| returnContent               |
  | search               | (no-op, returns empty)      |
  | set_bookmark         | setBookmarks                |
  | get_bookmark         | getBookmarks                |
  | download             | getContentResources + GET   |

DODP `contentID` is a string; Hummingbird uses `int`
node_ids. A per-user bidirectional id map (lifetime: one
hummingbird process) bridges the two.

Tests run against `httpx.MockTransport` -- no live DODP
server needed in CI. 23 tests across client + plugin.
