# Deployment

`openapi-dodp` ships as a container image to the kibble
registry on every `git tag v*`. It bakes the Hummingbird base
image + this plugin, so operators run **one image** instead of
running stock hummingbird and pip-installing the plugin at
deploy time.

## Pre-flight checklist

- [ ] A reachable DODP SOAP endpoint. Typical shape:
      `https://library.example.org/dodp/service`. Kolibre
      KADOS hosts at `/service` by default. The endpoint
      MUST be reachable from the container's network.
- [ ] Public hostname for hummingbird (eg.
      `dodp-bridge.cobd.ca`) with an A record pointing at
      the host. The service speaks plain HTTP on `:8000`
      behind your reverse proxy / TLS terminator.
- [ ] A persistent path for `/app/data` (bookshelves,
      sessions, bookmarks) and `/app/cache` (downloaded
      audio). The compose file declares named volumes;
      operators using `docker run` directly should
      bind-mount equivalents.
- [ ] Credentials the DODP backend recognises. The plugin
      forwards Basic-auth creds from hummingbird straight
      to DODP `logOn`; the upstream's user directory is
      the source of truth, this service holds no user db.

## Image distribution

`.github/workflows/release.yml` builds and pushes the image
on every `git tag v*`. Anonymous push to kibble; no secrets
to configure.

```sh
git tag -a v0.5.0 -m "Release 0.5.0"
git push origin v0.5.0
```

Within a couple of minutes:

- `kibble.apps.blindhub.ca/cobdfamily/openapi-dodp:0.5.0`
- `kibble.apps.blindhub.ca/cobdfamily/openapi-dodp:latest`

The Dockerfile derives from `kibble.apps.blindhub.ca/
cobdfamily/hummingbird:latest` — a hummingbird release
trickles down here on the next CI run (or the nightly).

## Configure

The plugin reads `OPENAPI_DODP_*` env vars. Defaults in
`src/openapi_dodp/config.py` are sized for KADOS-shape
deployments. For production, set:

| Variable | Required? | Notes |
| --- | --- | --- |
| `OPENAPI_DODP_BASE_URL` | **yes** | The DODP SOAP endpoint. Empty = plugin loads but every hook returns its empty fallback (and logs a warning at boot). |
| `OPENAPI_DODP_NAMESPACE` | no | Default `http://www.daisy.org/ns/daisy-online/`. Override only for non-conformant impls. |
| `OPENAPI_DODP_BOOKSHELF_LIST_ID` | no | DODP `getContentList` id used as the bookshelf. Default `issued`; KADOS uses this. Some servers use `current` or `loans`. |
| `OPENAPI_DODP_USER_AGENT` | no | Sent on every SOAP call. Default `openapi-dodp/0.x`. Some servers log this. |
| `OPENAPI_DODP_REQUEST_TIMEOUT_SECONDS` | no | Per-call timeout. Default 30s. Increase for slow `getContentList` on large libraries. |

Hummingbird's standalone-fallback creds (`HUMMINGBIRD_
USERNAME` / `HUMMINGBIRD_PASSWORD`) are unused once this
plugin's `authenticate()` runs — DODP is the auth source of
truth. Leave them blank in `docker-compose.yaml`.

## Run

`docker compose up -d` against the included
`docker-compose.yaml`:

```sh
cat > .env <<'EOF'
OPENAPI_DODP_BASE_URL=https://library.example.org/dodp/service
OPENAPI_DODP_HTTP_PORT=8000
OPENAPI_DODP_TAG=0.5.0
EOF

docker compose up -d
```

Or directly:

```sh
docker run -d --name openapi-dodp \
  -p 8000:8000 \
  -e OPENAPI_DODP_BASE_URL=https://library.example.org/dodp/service \
  -v openapi-dodp-data:/app/data \
  -v openapi-dodp-cache:/app/cache \
  kibble.apps.blindhub.ca/cobdfamily/openapi-dodp:latest
```

## Verify

```sh
# Liveness probe.
curl -fsS http://localhost:8000/
# {"service":"hummingbird","status":"ok","version":"0.x.y"}

# Login + bookshelf via REST. Replace alice/hunter2 with
# real DODP credentials for the upstream.
curl -fsSu alice:hunter2 \
  http://localhost:8000/protocols/hummingbird/v1/bookshelf/list

# Or via KADOS RPC (compatible with cobdfamily/openapi-kados):
curl -fsS http://localhost:8000/protocols/kados/v1/methods/logOn \
  -H 'Content-Type: application/json' \
  -d '{"args":{"username":"alice","password":"hunter2"}}'
```

If the bookshelf comes back empty even though the upstream
has books for the user, the most likely cause is the DODP
handshake — check `docker logs openapi-dodp` for a
`setReadingSystemAttributes failed` warning. Real KADOS
deployments occasionally reject the default reading-system
identification; raise an issue with the rejection message.

## Upgrade

```sh
# Pin to a specific version in .env then redeploy.
sed -i 's/^OPENAPI_DODP_TAG=.*/OPENAPI_DODP_TAG=0.5.1/' .env
docker compose pull
docker compose up -d
```

The image is the only thing that changes; bookshelves and
the audio cache survive in the named volumes. Rolling a tag
backwards is the same workflow with an older version
number.

## Common failure modes

- **"plugin not found" at boot.** Check that
  `HUMMINGBIRD_PLUGIN=openapi_dodp` is set. The Dockerfile
  sets it by default; an operator who passes
  `-e HUMMINGBIRD_PLUGIN=` (empty) explicitly disables it.

- **"OPENAPI_DODP_BASE_URL is not set" warning at boot,
  every request 401s.** The plugin loaded but has no
  upstream to talk to. Set the env var and restart.

- **All requests 401, even with correct creds.** The DODP
  server returned a SOAP fault to `logOn`. `docker logs`
  shows the `faultstring`. Common causes: bad credentials
  upstream, the server requires `setReadingSystemAttributes`
  to send a specific manufacturer/model string (override in
  a fork — config exposure is on the v0.6+ roadmap).

- **Bookshelf empty though the user has issued content.**
  The default `OPENAPI_DODP_BOOKSHELF_LIST_ID=issued`
  isn't what your DODP server calls its active loans list.
  Try `current`, `loans`, or check the upstream's
  `getServiceAttributes` response in the logs for the
  supported list IDs.

- **Downloads 404.** `getContentResources` returned no
  audio-shaped resources for the requested `fmt`. Logs
  show which mimes WERE available; the FORMAT_MAP table
  in `plugin.py` needs an entry covering the upstream's
  actual catalog.
