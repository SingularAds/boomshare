#!/usr/bin/env python
"""Send a correctly-signed Meta webhook to a running instance.

Every webhook must carry a valid `X-Hub-Signature-256`, so you cannot test the
endpoint with a plain curl. This builds a realistic payload, signs it with your
`META_APP_SECRET`, and posts it.

    # a customer messages you after clicking a click-to-WhatsApp ad
    python scripts/send_webhook.py message --text "I want to know more" --referral

    # the same customer replies
    python scripts/send_webhook.py message --text "what does it cost?"

    # a lead-ad form submission
    python scripts/send_webhook.py leadgen --leadgen-id LEAD-1001

    # a delivery receipt for a message you sent
    python scripts/send_webhook.py status --message-id wamid.XXXX --status read

    # the subscription handshake Meta performs when you save the webhook
    python scripts/send_webhook.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("httpx is required: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - .env loading is a convenience
    pass


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def message_payload(args: argparse.Namespace) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from": args.wa_id,
        "id": args.message_id or f"wamid.{uuid.uuid4().hex[:20].upper()}",
        "timestamp": str(int(time.time())),
        "type": "text",
        "text": {"body": args.text},
    }
    if args.referral:
        message["referral"] = {
            "source_url": "https://fb.me/boomshare",
            "source_id": args.ad_id,
            "source_type": "ad",
            "headline": "Record once, share everywhere",
            "body": "Try Boomshare free",
            "media_type": "image",
            "ctwa_clid": f"CLID-{uuid.uuid4().hex[:12]}",
        }

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-LOCAL",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": args.phone_number_id,
                            },
                            "contacts": [
                                {"profile": {"name": args.name}, "wa_id": args.wa_id}
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def leadgen_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "object": "page",
        "entry": [
            {
                "id": args.page_id,
                "time": int(time.time()),
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "created_time": int(time.time()),
                            "leadgen_id": args.leadgen_id,
                            "page_id": args.page_id,
                            "form_id": args.form_id,
                            "adgroup_id": "ADSET-LOCAL",
                            "ad_id": args.ad_id,
                        },
                    }
                ],
            }
        ],
    }


def status_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-LOCAL",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": args.phone_number_id},
                            "statuses": [
                                {
                                    "id": args.message_id,
                                    "status": args.status,
                                    "timestamp": str(int(time.time())),
                                    "recipient_id": args.wa_id,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _default_phone_number_id() -> str:
    """The first number from WHATSAPP_PHONE_NUMBER_IDS, or the sandbox default.

    Read straight from the environment rather than through Settings: this
    script is aimed at a *running* instance and must not need the application's
    full configuration to be valid.
    """
    raw = os.getenv("WHATSAPP_PHONE_NUMBER_IDS", "")
    try:
        ids = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        ids = []
    return str(ids[0]) if ids else "111222333"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("WEBHOOK_URL", "http://localhost:8000/webhooks/meta"))
    parser.add_argument("--secret", default=os.getenv("META_APP_SECRET", ""))
    parser.add_argument("--wa-id", default="919876543210", help="customer phone, E.164 without +")
    parser.add_argument(
        "--phone-number-id",
        default=_default_phone_number_id(),
        help="which of our numbers the message arrived on",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    msg = sub.add_parser("message", help="an inbound WhatsApp message")
    msg.add_argument("--text", default="I want to know more")
    msg.add_argument("--name", default="Priya")
    msg.add_argument("--message-id", default=None, help="reuse one to test idempotency")
    msg.add_argument("--referral", action="store_true", help="attach click-to-WhatsApp attribution")
    msg.add_argument("--ad-id", default="AD-LOCAL-1")

    lead = sub.add_parser("leadgen", help="a lead-ad form submission")
    lead.add_argument("--leadgen-id", default=f"LEAD-{uuid.uuid4().hex[:8].upper()}")
    lead.add_argument("--form-id", default="FORM-LOCAL-1")
    lead.add_argument("--ad-id", default="AD-LOCAL-2")
    lead.add_argument("--page-id", default="PAGE-LOCAL-1")

    status = sub.add_parser("status", help="a delivery receipt")
    status.add_argument("--message-id", required=True)
    status.add_argument("--status", default="delivered", choices=["sent", "delivered", "read", "failed"])

    sub.add_parser("verify", help="the GET subscription handshake")

    args = parser.parse_args()

    if args.command == "verify":
        token = os.getenv("META_VERIFY_TOKEN", "")
        if not token:
            return _fail("META_VERIFY_TOKEN is not set")
        response = httpx.get(
            args.url,
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "test-challenge-12345",
                "hub.verify_token": token,
            },
        )
        print(f"{response.status_code} {response.text}")
        return 0 if response.status_code == 200 else 1

    if not args.secret:
        return _fail("META_APP_SECRET is not set (put it in .env or pass --secret)")

    builders = {
        "message": message_payload,
        "leadgen": leadgen_payload,
        "status": status_payload,
    }
    payload = builders[args.command](args)
    body = json.dumps(payload).encode()

    response = httpx.post(
        args.url,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(args.secret, body)},
        timeout=15,
    )

    print(f"-> POST {args.url}")
    if args.command == "message":
        print(f"   message id: {payload['entry'][0]['changes'][0]['value']['messages'][0]['id']}")
    print(f"<- {response.status_code} {response.text}")
    return 0 if response.status_code == 200 else 1


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
