"""openapi-dodp -- Hummingbird plugin that proxies to a DODP backend.

Public surface: ``OpenapiDodpPlugin`` (registered via the
``hummingbird.plugins`` entry-point group as ``openapi_dodp``).
"""

from __future__ import annotations

__version__ = "0.10.0"

from .plugin import OpenapiDodpPlugin

__all__ = ["OpenapiDodpPlugin", "__version__"]
