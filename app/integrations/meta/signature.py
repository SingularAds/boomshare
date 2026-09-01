"""Meta webhook authentication.

Two separate mechanisms, both required:

  * GET  /webhooks/meta - subscription handshake, compares `hub.verify_token`
  * POST /webhooks/meta - every delivery carries `X-Hub-Signature-256`, an
    HMAC-SHA256 of the **raw** request body keyed by the app secret.

The signature must be computed over the exact bytes Meta sent. Never re-serialise
the parsed JSON first - key ordering and whitespace will differ and the check
will fail (or worse, be silently skipped).
"""

from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def compute_signature(app_secret: str, body: bytes) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def verify_signature(app_secret: str, body: bytes, header_value: str | None) -> bool:
    """Constant-time check of the `X-Hub-Signature-256` header."""
    if not app_secret or not header_value:
        return False
    if not header_value.startswith(_PREFIX):
        return False
    return hmac.compare_digest(compute_signature(app_secret, body), header_value)


def verify_subscription_token(expected: str, received: str | None) -> bool:
    if not expected or not received:
        return False
    return hmac.compare_digest(expected, received)
