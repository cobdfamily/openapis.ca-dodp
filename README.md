# openapi-dodp

Hummingbird plugin that proxies to a [DAISY Online Delivery
Protocol][dodp] (DODP) backend. Lets a [Hummingbird][hb] server
serve any DAISY library that speaks DODP — including a
[Kolibre KADOS][kados] instance — through Hummingbird's REST
surface, KADOS RPC, and the bookshelf/search/bookmark/download
abstractions shared with other plugins like
[`cobdfamily/nnels`][nnels].

The plugin keeps one DODP session (cookie jar) per Hummingbird
user. Sessions are process-local; on restart, clients
re-authenticate via Basic auth and the plugin re-issues `logOn`.

## How it relates

```
DAISY player ─ HTTP+Basic ─▶ Hummingbird ─ SOAP/DODP ─▶ KADOS / other DODP server
                                  │
                                  └─ openapi-dodp plugin
```

Compare to NNELS, which speaks Playwright/HTTP scraping to
nnels.ca. Same Hummingbird front; different backend protocol.

## Install

```sh
# Inside the hummingbird image / venv:
pip install openapi-dodp
```

For local development against an editable hummingbird:

```sh
uv sync
uv run pytest
```

## Configure

| Env var | Default | Purpose |
| --- | --- | --- |
| `HUMMINGBIRD_PLUGIN` | (unset) | Must be `openapi_dodp` to select this plugin. |
| `OPENAPI_DODP_BASE_URL` | (empty) | DODP SOAP endpoint URL. Required. |
| `OPENAPI_DODP_NAMESPACE` | `http://www.daisy.org/ns/daisy-online/` | Override only for non-conformant impls. |
| `OPENAPI_DODP_BOOKSHELF_LIST_ID` | `issued` | DODP `getContentList` id used as the bookshelf. |
| `OPENAPI_DODP_USER_AGENT` | `openapi-dodp/0.1` | Sent on every SOAP call. |
| `OPENAPI_DODP_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout in seconds. |

## Plugin hooks

| Hummingbird hook | DODP call |
| --- | --- |
| `authenticate` | `logOn` |
| `list_bookshelf` | `getContentList(id=issued)` |
| `add_to_bookshelf` | `issueContent` |
| `remove_from_bookshelf` | `returnContent` |
| `search` | not supported (returns empty) |
| `set_bookmark` | `setBookmarks` |
| `get_bookmark` | `getBookmarks` |
| `download` | `getContentResources` + stream first audio resource |

DODP v1 has no native search; v2's question-flow doesn't map
onto Hummingbird's flat list. `search` therefore returns an
empty `SearchResult` rather than raising.

## ID mapping

DODP `contentID` is a string ("con-12345"); Hummingbird's plugin
contract uses `int` node_ids. The plugin keeps a per-user
bidirectional map, handing out small ints stable for the life of
one Hummingbird process. Restart clears the map; clients re-list
the bookshelf and pick up the new ints transparently.

## License

AGPL-3.0 — see `LICENSE`.

[dodp]: https://www.daisy.org/activities/standards/daisy-online-delivery-protocol/
[hb]: https://github.com/cobdfamily/hummingbird
[kados]: https://github.com/kolibre/kolibre-kados
[nnels]: https://github.com/cobdfamily/nnels
