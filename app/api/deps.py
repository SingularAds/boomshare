"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


def _require_shared_secret(expected: str, presented: str | None, *, audience: str) -> None:
    """Constant-time check of a server-to-server shared secret.

    Refused outright when the secret was never configured - an empty token must
    never mean "open".
    """
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{audience} is not configured",
        )
    if not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Guards the install and activation events the Boomshare backend reports.

    This secret is held by a partner, so the endpoints behind it are the least
    it can be given: idempotent writes about one customer, and nothing that
    reads a conversation or removes data.
    """
    _require_shared_secret(
        get_settings().internal_api_token.get_secret_value(),
        x_internal_token,
        audience="internal API",
    )


async def require_admin_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Guards the operator console.

    A separate secret from the internal one, because the two are held by
    different people. What sits behind this reads every transcript, sends
    messages as us and can delete the database - so a partner integration
    losing its credential must not put any of that within reach.
    """
    _require_shared_secret(
        get_settings().admin_api_token.get_secret_value(),
        x_admin_token,
        audience="admin API",
    )


async def require_dashboard_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Guards the read-only dashboard viewer endpoints.

    Accepts either the lightweight dashboard_api_token (shareable with
    operators and stakeholders) or the master admin_api_token (so the team
    does not need two tokens to use their own dashboard). Rejected outright
    when neither is configured - an empty string must never mean "open".

    The dashboard token carries NO write authority - it cannot send messages,
    take over conversations, wipe data, or reload prompts. Those endpoints
    stay behind require_admin_token.
    """
    settings = get_settings()
    dashboard_secret = settings.dashboard_api_token.get_secret_value()
    admin_secret = settings.admin_api_token.get_secret_value()

    if not dashboard_secret and not admin_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard API is not configured",
        )

    presented = x_admin_token
    if not presented:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    # Accept the dedicated dashboard token when one is configured.
    if dashboard_secret and hmac.compare_digest(dashboard_secret, presented):
        return
    # Fall back to the master admin token (so the team never needs two tokens).
    if admin_secret and hmac.compare_digest(admin_secret, presented):
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
