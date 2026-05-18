"""openapis.ca-dodp -- Hummingbird plugin that proxies to a DODP backend.

Public surface: ``OpenapisDodpPlugin`` (registered via the
``hummingbird.plugins`` entry-point group as ``openapis_dodp``).
"""

from __future__ import annotations

__version__ = "0.1.0"

from .plugin import OpenapisDodpPlugin

__all__ = ["OpenapisDodpPlugin", "__version__"]
