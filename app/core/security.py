"""API-key authentication for the extraction endpoint."""

from __future__ import annotations

import secrets

from fastapi import Header

from app.config import get_settings
from app.core.exceptions import UnauthorizedError

API_KEY_HEADER = "X-API-Key"


def require_api_key(x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)) -> None:
    """FastAPI dependency enforcing ``X-API-Key``.

    Uses :func:`secrets.compare_digest` so the comparison does not leak the key through timing.
    """
    settings = get_settings()
    if not settings.has_api_key:
        # Failing closed: an unset key must never mean "allow everyone".
        raise UnauthorizedError("Server is not configured with an API key.")
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise UnauthorizedError()
