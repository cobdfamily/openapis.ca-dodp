# openapis.ca-dodp image: cobdfamily/hummingbird base + this
# plugin baked in. Mirrors the nnels image shape -- one tag,
# operator runs it directly, plugin is pre-installed, the
# HUMMINGBIRD_PLUGIN env var is set so the plugin is active on
# first request.
#
# Unlike nnels, this plugin has no native deps (no playwright,
# no chromium). The build is small: a pip install of this repo
# into the base image's venv pulls httpx + lxml + pydantic-
# settings (httpx is already present transitively in the base;
# lxml + pydantic-settings are small wheels with manylinux builds).
#
# Operators run:
#
#   docker run -d --name openapis-dodp \
#     -p 8000:8000 \
#     -e OPENAPIS_DODP_BASE_URL=https://library.example/dodp/service \
#     -v openapis-dodp-data:/app/data \
#     -v openapis-dodp-cache:/app/cache \
#     kibble.apps.blindhub.ca/cobdfamily/openapis.ca-dodp:latest
#
# Then point a DAISY player at the hummingbird HTTP surface via
# Basic auth -- credentials are forwarded to the DODP server as
# the logOn call.

ARG HUMMINGBIRD_TAG=latest
FROM kibble.apps.blindhub.ca/cobdfamily/hummingbird:${HUMMINGBIRD_TAG}

USER root

# The hummingbird base image's venv is uv-built and ships without
# pip; ensurepip is a one-time install for the source-install step
# below.
RUN /app/.venv/bin/python -m ensurepip --upgrade --default-pip

# Source layer. WORKDIR /app is set by the base image. Plugin
# install resolves hummingbird from the venv (already installed),
# and pulls in httpx + lxml + pydantic + pydantic-settings.
COPY --chown=hummingbird:hummingbird pyproject.toml README.md /app/openapis-ca-dodp-src/
COPY --chown=hummingbird:hummingbird src /app/openapis-ca-dodp-src/src

USER hummingbird

RUN /app/.venv/bin/python -m pip install --no-cache-dir /app/openapis-ca-dodp-src

# Activate the plugin by default. Operators override via
# `-e HUMMINGBIRD_PLUGIN=` to fall back to standalone hummingbird
# for debugging.
ENV HUMMINGBIRD_PLUGIN=openapis_dodp

# OPENAPIS_DODP_BASE_URL is intentionally NOT defaulted -- a
# missing URL is a misconfig the plugin reports via its startup
# warning log; defaulting to a placeholder would hide the
# misconfig in production. Operators MUST set it via
# `-e OPENAPIS_DODP_BASE_URL=...` or compose `environment:`.

# CMD inherited from the base image
# (uvicorn hummingbird.main:app --host 0.0.0.0 --port 8000) is
# preserved because we did not override ENTRYPOINT.
