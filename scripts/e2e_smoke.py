#!/usr/bin/env python
"""End-to-end smoke run of the whole system.

Starts a real uvicorn server and a real background worker in one process, runs
the migrations, and drives the complete customer journey over real HTTP with
correctly signed webhook payloads.

What is real here:
  * the FastAPI app, the ASGI server and the HTTP requests
  * the Alembic migrations and the database schema
  * the worker queue, job routing and scheduler sweeps
  * `MetaClient` and `OpenAiSalesModel` - the actual integration code, including
    URL building, auth headers, response parsing and error classification

What is substituted, and only at the network boundary:
  * graph.facebook.com  - we are not sending real WhatsApp messages
  * api.openai.com      - responses are scripted so the run is deterministic
  * PostgreSQL -> SQLite, Redis -> fakeredis, so this runs with no services

Run it with:  python scripts/e2e_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = Path(tempfile.gettempdir()) / f"boomshare_e2e_{uuid.uuid4().hex[:8]}.db"

os.environ.update(
    {
        "ENVIRONMENT": "local",
        "DATABASE_URL": f"sqlite+aiosqlite:///{DB_PATH}",
        "REDIS_URL": "redis://localhost:6379/15",
        "META_APP_SECRET": "smoke-app-secret",
        "META_VERIFY_TOKEN": "smoke-verify-token",
        "META_ACCESS_TOKEN": "smoke-access-token",
        "WHATSAPP_PHONE_NUMBER_ID": "111222333",
        "OPENAI_API_KEY": "sk-smoke",
        "INTERNAL_API_TOKEN": "smoke-internal-token",
        "DOWNLOAD_BASE_URL": "https://boomshare.ai/download",
        "LOG_LEVEL": "WARNING",
        "LOG_JSON": "false",
        "RUN_EMBEDDED_WORKER": "false",
        "SCHEDULER_INTERVAL_SECONDS": "1",
        "WEBHOOK_SWEEP_AFTER_SECONDS": "2",
    }
)

import fakeredis.aioredis  # noqa: E402
import httpx  # noqa: E402
import respx  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.core import redis as redis_helper  # noqa: E402
from scripts import _console  # noqa: E402

_console.setup()
from app.integrations.meta.signature import compute_signature  # noqa: E402

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"X-Internal-Token": "smoke-internal-token"}

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

failures: list[str] = []
whatsapp_outbox: list[dict] = []
openai_script: list[dict] = []


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def step(title: str) -> None:
    print(f"\n{BOLD}== {title}{RESET}")


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = f"{GREEN}PASS{RESET}" if condition else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    if not condition:
        failures.append(label)


def note(text: str) -> None:
    print(f"       {DIM}{text}{RESET}")


# --------------------------------------------------------------------------- #
# Scripted external services
# --------------------------------------------------------------------------- #
def script_reply(**decision) -> None:
    """Queue the next decision api.openai.com will return."""
    payload = {
        "reply_text": "",
        "intent": "information_request",
        "suggested_stage": None,
        "actions": [],
        "follow_up_minutes": None,
        "handoff_reason": None,
        "confidence": 0.85,
        "customer_notes": {},
    }
    payload.update(decision)
    openai_script.append(payload)


def openai_handler(request: httpx.Request) -> httpx.Response:
    decision = openai_script.pop(0) if openai_script else {
        "reply_text": "Happy to help - what would you like to know?",
        "intent": "information_request",
        "suggested_stage": None,
        "actions": [],
        "follow_up_minutes": None,
        "handoff_reason": None,
        "confidence": 0.5,
        "customer_notes": {},
    }
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(decision)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 950, "completion_tokens": 45, "total_tokens": 995},
        },
    )


def whatsapp_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("status") == "read":
        return httpx.Response(200, json={"success": True})

    message_id = f"wamid.OUT{len(whatsapp_outbox) + 1:04d}"
    whatsapp_outbox.append({**body, "_id": message_id})

    kind = body.get("type")
    if kind == "text":
        preview = body["text"]["body"].replace("\n", " ")
        print(f"  {DIM}-> WhatsApp to {body['to']}: {preview[:110]}{RESET}")
    else:
        template = body.get("template", {})
        print(f"  {DIM}-> WhatsApp to {body['to']}: [template {template.get('name')}]{RESET}")

    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": body["to"]}],
            "messages": [{"id": message_id}],
        },
    )


def ad_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "AD-CTWA-42",
            "name": "CTWA - Stop writing long explanations",
            "adset_id": "ADSET-9",
            "adset": {"name": "India / Support teams"},
            "campaign_id": "CAMP-CTWA",
            "campaign": {"name": "Boomshare Q1 Click-to-WhatsApp"},
        },
    )


def lead_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "LEAD-SMOKE-1",
            "created_time": "2026-01-05T10:00:00+0000",
            "ad_id": "AD-LEADFORM-9",
            "ad_name": "Boomshare Lead Ad - Managers",
            "adset_id": "ADSET-4",
            "adset_name": "India / Team leads",
            "campaign_id": "CAMP-Q1",
            "campaign_name": "Boomshare Q1 Lead Gen",
            "form_id": "FORM-77",
            "platform": "fb",
            "field_data": [
                {"name": "full_name", "values": ["Rahul Mehta"]},
                {"name": "phone_number", "values": ["+91 90000 11111"]},
                {"name": "email", "values": ["rahul@example.com"]},
                {"name": "company_size", "values": ["51-200"]},
            ],
        },
    )


# --------------------------------------------------------------------------- #
# Webhook payload builders
# --------------------------------------------------------------------------- #
def signed_post(client: httpx.AsyncClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        f"{BASE}/webhooks/meta",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": compute_signature("smoke-app-secret", body),
        },
    )


def inbound(text: str, wa_id="919876543210", name="Priya", message_id=None, referral=None) -> dict:
    message = {
        "from": wa_id,
        "id": message_id or f"wamid.IN{uuid.uuid4().hex[:12].upper()}",
        "timestamp": "1767600000",
        "type": "text",
        "text": {"body": text},
    }
    if referral:
        message["referral"] = referral
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-SMOKE",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "111222333"},
                            "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


CTWA_REFERRAL = {
    "source_url": "https://fb.me/boomshare",
    "source_id": "AD-CTWA-42",
    "source_type": "ad",
    "headline": "Stop writing long explanations",
    "body": "Record a 2 minute video instead",
    "media_type": "video",
    "ctwa_clid": "CLID-smoke-001",
}

LEADGEN = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE-SMOKE",
            "time": 1767600000,
            "changes": [
                {
                    "field": "leadgen",
                    "value": {
                        "created_time": 1767600000,
                        "leadgen_id": "LEAD-SMOKE-1",
                        "page_id": "PAGE-SMOKE",
                        "form_id": "FORM-77",
                        "adgroup_id": "ADSET-4",
                        "ad_id": "AD-LEADFORM-9",
                    },
                }
            ],
        }
    ],
}


# --------------------------------------------------------------------------- #
# Waiting for the worker
# --------------------------------------------------------------------------- #
async def drain(timeout: float = 15.0) -> None:
    """Wait until the worker has finished everything currently queued."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.models import WebhookEvent
    from app.worker import queue

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.15)
        if await queue.depth() > 0:
            continue
        async with session_scope() as session:
            unfinished = (
                await session.execute(
                    select(WebhookEvent.id).where(WebhookEvent.processed_at.is_(None))
                )
            ).first()
        if unfinished is None:
            await asyncio.sleep(0.15)
            return
    print(f"  {RED}timed out waiting for the worker{RESET}")


# --------------------------------------------------------------------------- #
# The journey
# --------------------------------------------------------------------------- #
async def run_journey() -> None:
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.core.clock import as_utc, utcnow
    from app.core.db import session_scope
    from app.models import (
        AiDecisionLog,
        Campaign,
        Conversation,
        Customer,
        DownloadLink,
        Lead,
        Message,
        Reminder,
        WebhookEvent,
    )

    async with httpx.AsyncClient(timeout=30) as http:
        # ---------------------------------------------------------------- #
        step("1. Service health")
        health = await http.get(f"{BASE}/health")
        check("GET /health returns ok", health.json().get("status") == "ok")

        ready = await http.get(f"{BASE}/health/ready")
        check("GET /health/ready reports the database", ready.json().get("database") == "ok")
        note(f"readiness: {ready.json()}")

        # ---------------------------------------------------------------- #
        step("2. Meta webhook subscription handshake")
        ok = await http.get(
            f"{BASE}/webhooks/meta",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "smoke-challenge",
                "hub.verify_token": "smoke-verify-token",
            },
        )
        check("correct verify token echoes the challenge", ok.text == "smoke-challenge")

        bad = await http.get(
            f"{BASE}/webhooks/meta",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "smoke-challenge",
                "hub.verify_token": "wrong",
            },
        )
        check("wrong verify token is refused", bad.status_code == 403)

        # ---------------------------------------------------------------- #
        step("3. Webhook signature enforcement")
        unsigned = await http.post(f"{BASE}/webhooks/meta", json=inbound("hello"))
        check("unsigned delivery is refused", unsigned.status_code == 403)

        forged = await http.post(
            f"{BASE}/webhooks/meta",
            content=json.dumps(inbound("hello")).encode(),
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
        check("forged signature is refused", forged.status_code == 403)

        async with session_scope() as session:
            stored = (await session.execute(select(func.count()).select_from(WebhookEvent))).scalar()
        check("nothing was stored from refused deliveries", stored == 0)

        # ---------------------------------------------------------------- #
        step("4. FLOW 1 - customer clicks a click-to-WhatsApp ad and messages us")
        script_reply(
            reply_text="Hey Priya! What kind of things are you explaining over and over at the moment?",
            intent="information_request",
            suggested_stage="engaged",
            customer_notes={"source": "ctwa ad about long explanations"},
        )
        first = inbound("I want to know more", referral=CTWA_REFERRAL, message_id="wamid.SMOKE1")
        response = await signed_post(http, first)
        check("webhook accepted", response.json() == {"received": 1, "accepted": 1})
        await drain()

        async with session_scope() as session:
            customer = (await session.execute(select(Customer))).scalar_one()
            conversation = (await session.execute(select(Conversation))).scalar_one()
            lead = (await session.execute(select(Lead))).scalar_one()
            messages = list((await session.execute(select(Message))).scalars())

        check("customer created from the phone number", customer.phone == "919876543210")
        check("WhatsApp profile name captured", customer.full_name == "Priya")
        check("conversation opened and AI-handled", conversation.handling_mode == "ai")
        check("sales stage advanced to engaged", conversation.sales_stage == "engaged",
              f"stage={conversation.sales_stage}")
        check("ad attribution recorded on the lead", lead.ctwa_clid == "CLID-smoke-001")
        check("inbound and outbound both persisted", len(messages) == 2)
        check("a WhatsApp reply was sent", len(whatsapp_outbox) == 1)
        note(f"lead source={lead.source}  ad headline={(lead.raw_payload or {}).get('headline')}")

        async with session_scope() as session:
            lead = (await session.execute(select(Lead))).scalar_one()
            campaign = (
                await session.execute(
                    select(Campaign).where(Campaign.meta_campaign_id == "CAMP-CTWA")
                )
            ).scalar_one_or_none()
        check("the ad was resolved to its campaign via the Graph API", campaign is not None)
        check("the lead was backfilled with that campaign",
              campaign is not None and lead.campaign_id == campaign.id)

        # ---------------------------------------------------------------- #
        step("5. Idempotency - Meta redelivers the identical webhook")
        replay = await signed_post(http, first)
        await drain()
        check("duplicate accepted but not reprocessed", replay.json()["accepted"] == 0)
        check("no second reply was sent", len(whatsapp_outbox) == 1)

        async with session_scope() as session:
            count = (await session.execute(select(func.count()).select_from(Message))).scalar()
            customers = (await session.execute(select(func.count()).select_from(Customer))).scalar()
        check("no duplicate message row", count == 2)
        check("no duplicate customer", customers == 1)

        # ---------------------------------------------------------------- #
        step("6. The conversation continues")
        script_reply(
            reply_text="There is a free plan with no card needed - unlimited 5 minute recordings.",
            intent="pricing_question",
            suggested_stage="product_explained",
        )
        await signed_post(http, inbound("how much does it cost?"))
        await drain()
        check("second reply sent", len(whatsapp_outbox) == 2)

        async with session_scope() as session:
            conversation = (await session.execute(select(Conversation))).scalar_one()
        check("stage moved to product_explained", conversation.sales_stage == "product_explained")

        # ---------------------------------------------------------------- #
        step("7. The AI must not invent a download URL")
        script_reply(
            reply_text="Sure - grab it from https://totally-not-boomshare.example/get and you are set.",
            intent="download_request",
            actions=["send_download_link"],
            suggested_stage="download_suggested",
        )
        await signed_post(http, inbound("ok send me the link"))
        await drain()

        sent_body = whatsapp_outbox[-1]["text"]["body"]
        async with session_scope() as session:
            link = (await session.execute(select(DownloadLink))).scalar_one()
            conversation = (await session.execute(select(Conversation))).scalar_one()
            decisions = list((await session.execute(select(AiDecisionLog))).scalars())

        check("the hallucinated URL was stripped", "totally-not-boomshare" not in sent_body)
        check("the backend's tracked link was attached", link.url in sent_body)
        check("the link carries an attribution token", link.token in sent_body)
        check("stage is link_sent", conversation.sales_stage == "link_sent")
        check("link is marked sent but NOT downloaded", link.sent_at is not None and link.downloaded_at is None)
        check("the rejection was recorded for prompt tuning",
              any("url_in_reply" in (d.rejected_reasons or {}) for d in decisions))
        note(f"message sent: {sent_body[:150]}")

        # ---------------------------------------------------------------- #
        step("8. The AI cannot claim an install happened")
        script_reply(
            reply_text="Great, you are all set up then.",
            intent="buying_intent",
            suggested_stage="activated",
        )
        await signed_post(http, inbound("installed it already"))
        await drain()

        async with session_scope() as session:
            customer = (await session.execute(select(Customer))).scalar_one()
            conversation = (await session.execute(select(Conversation))).scalar_one()
            decisions = list((await session.execute(select(AiDecisionLog))).scalars())

        check("customer is still not marked as downloaded", customer.downloaded_at is None)
        check("stage did not jump to activated", conversation.sales_stage != "activated")
        check("the refused stage was logged",
              any("stage_system_only" in (d.rejected_reasons or {}) for d in decisions))

        # ---------------------------------------------------------------- #
        step("9. Boomshare's backend confirms the real install")
        async with session_scope() as session:
            link = (await session.execute(select(DownloadLink))).scalar_one()
            token = link.token

        unauth = await http.post(f"{BASE}/internal/events/download", json={"token": token})
        check("internal API requires a token", unauth.status_code == 401)

        download = await http.post(
            f"{BASE}/internal/events/download",
            json={"token": token, "platform": "windows", "app_version": "1.4.2"},
            headers=AUTH,
        )
        check("download event applied", download.json()["applied"] is True)

        repeat = await http.post(
            f"{BASE}/internal/events/download", json={"token": token}, headers=AUTH
        )
        check("a repeated download event is a no-op", repeat.json()["applied"] is False)

        activation = await http.post(
            f"{BASE}/internal/events/activation", json={"token": token}, headers=AUTH
        )
        check("activation event applied", activation.json()["applied"] is True)

        async with session_scope() as session:
            customer = (await session.execute(select(Customer))).scalar_one()
            conversation = (await session.execute(select(Conversation))).scalar_one()

        check("customer marked downloaded", customer.downloaded_at is not None)
        check("customer marked activated", customer.activated_at is not None)
        check("stage is activated", conversation.sales_stage == "activated")

        # ---------------------------------------------------------------- #
        step("10. FLOW 2 - a Meta lead ad submission")
        before = len(whatsapp_outbox)
        response = await signed_post(http, LEADGEN)
        check("leadgen webhook accepted", response.json()["accepted"] == 1)
        await drain()

        async with session_scope() as session:
            lead = (
                await session.execute(select(Lead).where(Lead.meta_leadgen_id == "LEAD-SMOKE-1"))
            ).scalar_one()
            campaign = (
                await session.execute(select(Campaign).where(Campaign.meta_campaign_id == "CAMP-Q1"))
            ).scalar_one()
            new_customer = (
                await session.execute(select(Customer).where(Customer.phone == "919000011111"))
            ).scalar_one()
            reminders = list((await session.execute(select(Reminder))).scalars())

        check("lead retrieved from the Graph API and stored", lead.field_data["company_size"] == "51-200")
        check("phone normalised from the form format", new_customer.phone == "919000011111")
        check("campaign attribution stored", campaign.name == "Boomshare Q1 Lead Gen")
        check("first contact used a TEMPLATE, not free text",
              whatsapp_outbox[-1].get("type") == "template",
              f"type={whatsapp_outbox[-1].get('type')}")
        check("template addressed the lead by name",
              whatsapp_outbox[-1]["template"]["components"][0]["parameters"][0]["text"] == "Rahul")
        check("a no-reply nudge was scheduled", any(r.kind == "no_reply_nudge" for r in reminders))
        check("exactly one outbound message for the lead", len(whatsapp_outbox) == before + 1)

        # ---------------------------------------------------------------- #
        step("11. The lead replies and the AI conversation starts")
        script_reply(
            reply_text="Thanks for getting in touch Rahul - what does your team spend most time explaining?",
            intent="greeting",
            suggested_stage="engaged",
        )
        await signed_post(http, inbound("yes tell me more", wa_id="919000011111", name="Rahul Mehta"))
        await drain()

        async with session_scope() as session:
            conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.customer_id == new_customer.id)
                )
            ).scalar_one()
            reminders = list(
                (
                    await session.execute(
                        select(Reminder).where(Reminder.conversation_id == conversation.id)
                    )
                ).scalars()
            )

        check("free-form AI reply now allowed", whatsapp_outbox[-1].get("type") == "text")
        check("stage moved to engaged", conversation.sales_stage == "engaged")
        nudges = [r for r in reminders if r.kind == "no_reply_nudge"]
        check("the nudge was cancelled because they replied",
              nudges and all(r.status == "cancelled" for r in nudges),
              f"statuses={[str(r.status) for r in reminders]}")
        check("the turn still left a way back into the conversation",
              any(r.status == "pending" for r in reminders))

        # ---------------------------------------------------------------- #
        step("12. Follow-up scheduling")
        script_reply(
            reply_text="No problem at all - shall I check back on Thursday?",
            intent="information_request",
            actions=["schedule_follow_up"],
            follow_up_minutes=2880,
        )
        await signed_post(http, inbound("busy right now, ping me later", wa_id="919000011111"))
        await drain()

        async with session_scope() as session:
            conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.customer_id == new_customer.id)
                )
            ).scalar_one()
            pending = list(
                (
                    await session.execute(
                        select(Reminder).where(
                            Reminder.conversation_id == conversation.id,
                            Reminder.status == "pending",
                        )
                    )
                ).scalars()
            )
        check("exactly one follow-up is queued in PostgreSQL", len(pending) == 1,
              f"pending={len(pending)}")
        if pending:
            due_in = as_utc(pending[0].due_at) - utcnow()
            check("the AI's own delay replaced the automatic check-in",
                  timedelta(hours=47) < due_in <= timedelta(hours=48),
                  f"due in {due_in}")
            note(f"due at {pending[0].due_at} - reason: {pending[0].reason}")

        # ---------------------------------------------------------------- #
        step("13. Human handoff")
        script_reply(
            reply_text="Of course - let me get a colleague to help with that.",
            intent="human_request",
            actions=["request_human_handoff"],
            handoff_reason="customer asked for a person",
        )
        await signed_post(http, inbound("can I speak to a real person", wa_id="919000011111"))
        await drain()

        async with session_scope() as session:
            conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.customer_id == new_customer.id)
                )
            ).scalar_one()
            conversation_id = conversation.id

        check("conversation is now human-handled", conversation.handling_mode == "human")
        check("the acknowledgement was still sent", "colleague" in whatsapp_outbox[-1]["text"]["body"])

        sent_before = len(whatsapp_outbox)
        await signed_post(http, inbound("hello? anyone there?", wa_id="919000011111"))
        await drain()
        check("the AI stays silent for a human-handled conversation",
              len(whatsapp_outbox) == sent_before)

        async with session_scope() as session:
            inbound_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == conversation_id, Message.direction == "inbound")
                )
            ).scalar()
        check("but the customer's message is still stored for the agent", inbound_count == 4,
              f"inbound={inbound_count}")

        # ---------------------------------------------------------------- #
        step("14. Operator console")
        listing = await http.get(f"{BASE}/admin/conversations", headers=AUTH)
        check("conversations can be listed", len(listing.json()) == 2)

        detail = await http.get(f"{BASE}/admin/conversations/{conversation_id}", headers=AUTH)
        check("the full transcript is available", len(detail.json()["messages"]) >= 5)

        agent_reply = await http.post(
            f"{BASE}/admin/conversations/{conversation_id}/messages",
            json={"body": "Hi Rahul, Sam here from Boomshare.", "agent": "sam@boomshare.ai"},
            headers=AUTH,
        )
        check("an agent can reply as a human", agent_reply.json()["ai_generated"] is False)
        check("the agent's message actually went to WhatsApp",
              whatsapp_outbox[-1]["text"]["body"].startswith("Hi Rahul, Sam here"))

        release = await http.post(
            f"{BASE}/admin/conversations/{conversation_id}/release", json={}, headers=AUTH
        )
        check("the conversation can be handed back to the AI",
              release.json()["handling_mode"] == "ai")

        # ---------------------------------------------------------------- #
        step("15. Attribution reporting")
        funnel = await http.get(f"{BASE}/admin/reports/funnel", headers=AUTH)
        rows = {row["campaign_name"] or "unattributed": row for row in funnel.json()}
        check("the funnel report returns rows", len(rows) >= 1)
        for name, row in rows.items():
            note(
                f"{name}: leads={row['leads']} conversations={row['conversations']} "
                f"links_sent={row['links_sent']} downloads={row['downloads']} "
                f"activations={row['activations']}"
            )
        check("the click-to-WhatsApp install is attributed",
              any(row["activations"] == 1 for row in rows.values()))

        # ---------------------------------------------------------------- #
        step("16. Delivery receipts")
        first_id = whatsapp_outbox[0]["_id"]
        receipt = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA-SMOKE",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "111222333"},
                                "statuses": [
                                    {
                                        "id": first_id,
                                        "status": "read",
                                        "timestamp": "1767600100",
                                        "recipient_id": "919876543210",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        await signed_post(http, receipt)
        await drain()

        async with session_scope() as session:
            message = (
                await session.execute(
                    select(Message).where(Message.provider_message_id == first_id)
                )
            ).scalar_one()
        check("delivery receipt applied to the message", message.status == "read")
        check("read timestamp recorded", message.read_at is not None)

        # ---------------------------------------------------------------- #
        step("17. Resilience - OpenAI outage")
        openai_route.mock(return_value=httpx.Response(503, json={"error": {"message": "overloaded"}}))
        sent_before = len(whatsapp_outbox)
        await signed_post(http, inbound("are you still there?", message_id="wamid.OUTAGE"))

        # Wait for the first attempt to fail, then inspect - the scheduler will
        # keep retrying in the background, so assert the invariant (never
        # finished, error recorded), not a momentary status value.
        for _ in range(40):
            await asyncio.sleep(0.25)
            async with session_scope() as session:
                event = (
                    await session.execute(
                        select(WebhookEvent).where(WebhookEvent.event_key == "wamid.OUTAGE")
                    )
                ).scalar_one()
            if event.attempts >= 1 and event.last_error:
                break

        check("no reply was invented while the model was down",
              len(whatsapp_outbox) == sent_before)
        check("the failure was recorded", bool(event.last_error), event.last_error or "")
        check("the event was not marked done", event.processed_at is None)

        async with session_scope() as session:
            stored = (
                await session.execute(
                    select(Message).where(Message.content == "are you still there?")
                )
            ).scalar_one_or_none()
        check("the customer's message was still stored", stored is not None)

        step("18. Recovery - OpenAI comes back and the sweeper retries")
        openai_route.mock(side_effect=openai_handler)
        script_reply(reply_text="Still here! Sorry about that - where were we?", intent="greeting")

        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            if len(whatsapp_outbox) > sent_before:
                break

        check("the scheduler retried it without anyone intervening",
              len(whatsapp_outbox) > sent_before)
        if len(whatsapp_outbox) > sent_before:
            note(f"recovered reply: {whatsapp_outbox[-1]['text']['body'][:100]}")

        # ---------------------------------------------------------------- #
        step("19. The download link is not held back to qualify anyone")
        buyer = "919000022222"
        script_reply(
            reply_text="Here you go - are you on Windows or a Mac?",
            intent="buying_intent",
            actions=["send_download_link"],
            suggested_stage="download_suggested",
        )
        await signed_post(http, inbound("sounds useful actually", wa_id=buyer, name="Asha Rao"))
        await drain()

        async with session_scope() as session:
            buyer_customer = (
                await session.execute(select(Customer).where(Customer.wa_id == buyer))
            ).scalar_one()
            buyer_link = (
                await session.execute(
                    select(DownloadLink).where(DownloadLink.customer_id == buyer_customer.id)
                )
            ).scalar_one()

        body = whatsapp_outbox[-1]["text"]["body"]
        check("soft interest gets the link on the same turn", buyer_link.url in body)
        check("the build question rides along with it", "Windows or a Mac" in body)
        check("the link is marked sent", buyer_link.sent_at is not None)
        note(f"message sent: {body[:150]}")

        # ---------------------------------------------------------------- #
        step("20. \"Not right now\" books a callback instead of killing the lead")
        script_reply(
            reply_text="Sounds good! I'll check back with you in 5 minutes.",
            intent="information_request",
            # Exactly what the model returned live: a callback and a write-off
            # in the same decision.
            actions=["schedule_follow_up", "mark_not_interested"],
            suggested_stage="not_interested",
            follow_up_minutes=5,
        )
        await signed_post(http, inbound("After 5 minutes I will have", wa_id=buyer))
        await drain()

        async with session_scope() as session:
            buyer_conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.customer_id == buyer_customer.id)
                )
            ).scalar_one()
            callback = (
                await session.execute(
                    select(Reminder).where(
                        Reminder.conversation_id == buyer_conversation.id,
                        Reminder.status == "pending",
                    )
                )
            ).scalar_one()

        due_in = as_utc(callback.due_at) - utcnow()
        check("the lead was not written off", buyer_conversation.sales_stage != "not_interested")
        check("the callback is booked for five minutes, not an hour",
              timedelta(minutes=4) < due_in <= timedelta(minutes=5),
              f"due in {due_in}")

        # The scheduler picks it up on its own once it comes due.
        async with session_scope() as session:
            row = await session.get(Reminder, callback.id)
            row.due_at = utcnow() - timedelta(seconds=1)

        script_reply(
            reply_text="Ready when you are - want to grab it now?",
            intent="information_request",
        )
        sent_before = len(whatsapp_outbox)
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            if len(whatsapp_outbox) > sent_before:
                break

        async with session_scope() as session:
            delivered = await session.get(Reminder, callback.id)

        check("the promised message actually went out", len(whatsapp_outbox) > sent_before)
        check("the reminder is resolved as sent", delivered.status == "sent")
        if len(whatsapp_outbox) > sent_before:
            note(f"callback sent: {whatsapp_outbox[-1]['text']['body'][:100]}")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
async def main() -> int:
    from app.main import create_app
    from app.worker.runner import consume, schedule

    redis_helper.set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))

    config = uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(100):
        await asyncio.sleep(0.05)
        if server.started:
            break
    else:
        print(f"{RED}server did not start{RESET}")
        return 1

    stop = asyncio.Event()
    workers = [asyncio.create_task(consume(stop)), asyncio.create_task(schedule(stop))]

    try:
        await run_journey()
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        await redis_helper.close_redis()
        from app.core.db import dispose_engine

        await dispose_engine()

    print()
    if failures:
        print(f"{RED}{BOLD}{len(failures)} check(s) failed:{RESET}")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"{GREEN}{BOLD}All checks passed.{RESET}")
    print(f"{DIM}WhatsApp messages sent during the run: {len(whatsapp_outbox)}{RESET}")
    return 0


if __name__ == "__main__":
    print(f"{BOLD}Boomshare AI - end-to-end smoke run{RESET}")
    print(f"{DIM}database: {DB_PATH}{RESET}")

    alembic_config = Config(str(ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(alembic_config, "head")
    print(f"{DIM}migrations applied{RESET}")

    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1").pass_through()
        router.route(host="localhost").pass_through()
        openai_route = router.post("https://api.openai.com/v1/chat/completions")
        openai_route.mock(side_effect=openai_handler)
        router.post(url__regex=r"https://graph\.facebook\.com/.+/messages").mock(
            side_effect=whatsapp_handler
        )
        router.get(url__regex=r"https://graph\.facebook\.com/.+/LEAD-SMOKE-1").mock(
            side_effect=lead_handler
        )
        router.get(url__regex=r"https://graph\.facebook\.com/[^/]+/AD-CTWA-42.*").mock(
            side_effect=ad_handler
        )

        exit_code = asyncio.run(main())

    DB_PATH.unlink(missing_ok=True)
    raise SystemExit(exit_code)
