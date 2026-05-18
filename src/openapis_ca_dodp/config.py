"""Plugin config.

Reads OPENAPIS_DODP_* env vars (and a .env at the working dir
when present) so an operator can switch the backend without
rebuilding the image. Empty ``base_url`` is a fatal misconfig
-- the plugin refuses to start so an operator sees the problem
at boot, not on first request.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENAPIS_DODP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # URL of the DODP SOAP endpoint. Typical shape:
    #   https://library.example.org/dodp/service
    # KADOS exposes it at /service by default.
    base_url: str = Field(default="")

    # DODP namespace. v1 is "http://www.daisy.org/ns/daisy-online/";
    # v2.0.2 (the version Kolibre KADOS speaks today) is the same
    # namespace string -- the protocol version distinction lives in
    # the response payloads, not the namespace itself. Override
    # only if pointing at a custom impl that re-namespaced.
    namespace: str = "http://www.daisy.org/ns/daisy-online/"

    # The DODP-side "issued" content list is what Hummingbird calls
    # the bookshelf. Some servers expose synonyms (e.g. "current",
    # "loans"). Keep this configurable for vendor quirks.
    bookshelf_list_id: str = "issued"

    # User-Agent header on every SOAP call. Some DODP servers
    # require an identifying string (DAISY clients usually send
    # the player's model name + firmware).
    user_agent: str = "openapis.ca-dodp/0.1"

    # Per-call timeout for the SOAP HTTP request. Generous default
    # because some DODP servers are slow on getContentList for
    # large libraries.
    request_timeout_seconds: float = 30.0


settings = Settings()
