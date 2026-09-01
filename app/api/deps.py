"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Guards endpoints called by the Boomshare desktop backend and admin tools.

    A shared secret is proportionate here: these are server-to-server calls
    inside our own estate, not user-facing authentication. Compared in constant
    time, and refused outright if the secret was never configured - an empty
    token must never mean "open".
    """
    expected = get_settings().internal_api_token.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal API is not configured",
        )
    if not x_internal_token or not hmac.compare_digest(expected, x_internal_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
