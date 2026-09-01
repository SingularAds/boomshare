#!/usr/bin/env python
"""Interactive local sandbox - the whole system, no Meta account needed.

Starts, in one process:

  * the fake Meta Graph API   (127.0.0.1:8090)  - validates like the real one
  * the Boomshare API         (127.0.0.1:8000)  - the real app
  * the background worker                        - the real worker

You then type as the customer. Every message is signed and posted to the real
webhook endpoint over real HTTP, and you watch the pipeline run.

    python scripts/sandbox.py                 # SQLite + fakeredis + canned AI
    python scripts/sandbox.py --real-ai       # use your real OPENAI_API_KEY
    python scripts/sandbox.py --postgres      # use DATABASE_URL / REDIS_URL from .env
    python scripts/sandbox.py --quiet         # transcript only, no pipeline trace

Type /help once inside for the command list.

The only thing not real here is Meta itself - and `scripts/fake_meta.py`
enforces Meta's actual rules and returns Meta's actual error codes, so a flow
that works here works against a real WhatsApp Business Account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# Configuration, before anything imports app.core.config
# --------------------------------------------------------------------------- #
parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--real-ai", action="store_true", help="call the real OpenAI API")
parser.add_argument("--postgres", action="store_true", help="use DATABASE_URL/REDIS_URL from .env")
parser.add_argument("--quiet", action="store_true", help="hide the pipeline trace")
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--meta-port", type=int, default=8090)
ARGS = parser.parse_args()

if ARGS.postgres:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

DB_PATH = Path(tempfile.gettempdir()) / f"boomshare_sandbox_{uuid.uuid4().hex[:8]}.db"

_env = {
    "ENVIRONMENT": "local",
    "META_APP_SECRET": "sandbox-app-secret",
    "META_VERIFY_TOKEN": "sandbox-verify-token",
    "META_ACCESS_TOKEN": "sandbox-access-token",
    "META_GRAPH_BASE_URL": f"http://127.0.0.1:{ARGS.meta_port}",
    "WHATSAPP_PHONE_NUMBER_ID": "111222333",
    "INTERNAL_API_TOKEN": "sandbox-internal-token",
    "DOWNLOAD_BASE_URL": "https://boomshare.ai/download",
    "LOG_LEVEL": "WARNING",
    "LOG_JSON": "false",
    "SCHEDULER_INTERVAL_SECONDS": "2",
    "WEBHOOK_SWEEP_AFTER_SECONDS": "5",
}
if not ARGS.postgres:
    _env["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"
if not ARGS.real_ai:
    _env["OPENAI_API_KEY"] = "sk-sandbox"

os.environ.update(_env)
if not ARGS.quiet:
    os.environ["TRACE"] = "1"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core import trace as tracing  # noqa: E402
from app.integrations.meta.signature import compute_signature  # noqa: E402
from scripts import _console  # noqa: E402

_console.setup()

API = f"http://127.0.0.1:{ARGS.port}"
META = f"http://127.0.0.1:{ARGS.meta_port}"
AUTH = {"X-Internal-Token": "sandbox-internal-token"}

BOLD, DIM, RESET = _console.BOLD, _console.DIM, _console.RESET
GREEN, RED, CYAN, YELLOW, BLUE = (
    _console.GREEN,
    _console.RED,
    _console.CYAN,
    _console.YELLOW,
    _console.BLUE,
)

WA_ID = "919876543210"
NAME = "Priya"
last_webhook: dict | None = None
last_outbound_id: str | None = None


# --------------------------------------------------------------------------- #
# A canned AI, so the sandbox runs with no OpenAI key
# --------------------------------------------------------------------------- #
class CannedSalesModel:
    """Keyword-driven replies. Enough to exercise every branch of the pipeline.

    Not a simulation of the real model's judgement - a way to drive the
    application deterministically. Use --real-ai to see actual sales behaviour.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, messages):
        from app.ai.schemas import AiCallResult, AiDecision

        self.calls += 1
        last_user = ""
        for m in reversed(messages):
            if m["role"] == "user":
                last_user = m["content"].lower()
                break

        d = dict(reply_text="", intent="information_request", confidence=0.85)

        if any(w in last_user for w in ("stop", "unsubscribe", "leave me alone")):
            d.update(
                reply_text="Understood - I won't message you again. All the best.",
                intent="opt_out",
                actions=["opt_out"],
            )
        elif any(w in last_user for w in ("human", "real person", "agent", "someone")):
            d.update(
                reply_text="Of course - let me bring in a colleague who can help with that.",
                intent="human_request",
                actions=["request_human_handoff"],
                handoff_reason="customer asked for a person",
            )
        elif any(w in last_user for w in ("link", "download", "install", "try it", "get it")):
            d.update(
                reply_text="Great - here you go. Takes about a minute to install.",
                intent="download_request",
                actions=["send_download_link"],
                suggested_stage="download_suggested",
            )
        elif any(w in last_user for w in ("later", "busy", "next week", "not now")):
            d.update(
                reply_text="No problem at all. Want me to check back in a couple of days?",
                intent="objection",
                actions=["schedule_follow_up"],
                follow_up_minutes=2880,
                suggested_stage="objection_handling",
            )
        elif any(w in last_user for w in ("price", "cost", "pricing", "how much", "free")):
            d.update(
                reply_text=(
                    "There's a free plan - unlimited 5 minute recordings, 25 videos, no card "
                    "needed. Paid plans lift those limits."
                ),
                intent="pricing_question",
                suggested_stage="product_explained",
            )
        elif any(w in last_user for w in ("loom", "compare", "different", "versus", "vs ")):
            d.update(
                reply_text=(
                    "Same idea - record, get a link, send it. Boomshare adds a free tier with no "
                    "card, editing without re-recording, and viewer analytics lower down the plans."
                ),
                intent="comparison",
                suggested_stage="product_explained",
            )
        elif "linux" in last_user or "phone" in last_user or "mobile" in last_user:
            d.update(
                reply_text="Not today, I'm afraid - Windows and macOS only, no mobile recording app.",
                intent="feature_question",
                suggested_stage="product_explained",
            )
        elif self.calls == 1:
            d.update(
                reply_text=f"Hey {NAME}! What are you explaining over and over at the moment?",
                intent="greeting",
                suggested_stage="engaged",
            )
        else:
            d.update(
                reply_text="Got it. What does your team spend the most time explaining?",
                intent="information_request",
                suggested_stage="qualified",
            )

        decision = AiDecision.model_validate(d)
        return AiCallResult(
            decision=decision,
            model="canned-sandbox",
            raw_response=decision.model_dump(mode="json"),
            prompt_tokens=900,
            completion_tokens=40,
            latency_ms=5,
        )


# --------------------------------------------------------------------------- #
# Webhook payloads
# --------------------------------------------------------------------------- #
CTWA_REFERRAL = {
    "source_url": "https://fb.me/boomshare",
    "source_id": "AD-CTWA-42",
    "source_type": "ad",
    "headline": "Stop writing long explanations",
    "body": "Record a 2 minute video instead",
    "media_type": "video",
    "ctwa_clid": "CLID-sandbox-001",
}


def inbound_payload(text: str, *, referral: dict | None = None, message_id: str | None = None) -> dict:
    message = {
        "from": WA_ID,
        "id": message_id or f"wamid.IN{uuid.uuid4().hex[:14].upper()}",
        "timestamp": str(int(time.time())),
        "type": "text",
        "text": {"body": text},
    }
    if referral:
        message["referral"] = referral
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-SANDBOX",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": "111222333",
                            },
                            "contacts": [{"profile": {"name": NAME}, "wa_id": WA_ID}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


LEADGEN_PAYLOAD = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE-SANDBOX",
            "time": int(time.time()),
            "changes": [
                {
                    "field": "leadgen",
                    "value": {
                        "created_time": int(time.time()),
                        "leadgen_id": "LEAD-SANDBOX-1",
                        "page_id": "PAGE-SANDBOX",
                        "form_id": "FORM-77",
                        "adgroup_id": "ADSET-4",
                        "ad_id": "AD-LEADFORM-9",
                    },
                }
            ],
        }
    ],
}


def status_payload(message_id: str, state: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-SANDBOX",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "111222333"},
                            "statuses": [
                                {
                                    "id": message_id,
                                    "status": state,
                                    "timestamp": str(int(time.time())),
                                    "recipient_id": WA_ID,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def post_webhook(http: httpx.AsyncClient, payload: dict) -> httpx.Response:
    global last_webhook
    last_webhook = payload
    body = json.dumps(payload).encode()
    return await http.post(
        f"{API}/webhooks/meta",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": compute_signature("sandbox-app-secret", body),
        },
    )


# --------------------------------------------------------------------------- #
# Waiting and reporting
# --------------------------------------------------------------------------- #
async def wait_for_worker(timeout: float = 30.0) -> None:
    from app.core.db import session_scope
    from app.models import WebhookEvent
    from app.worker import queue

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.12)
        if await queue.depth() > 0:
            continue
        async with session_scope() as session:
            pending = (
                await session.execute(
                    select(WebhookEvent.id).where(WebhookEvent.processed_at.is_(None))
                )
            ).first()
        if pending is None:
            await asyncio.sleep(0.15)
            return
    print(f"  {RED}worker did not finish within {timeout:.0f}s{RESET}")


async def show_new_messages(http: httpx.AsyncClient, since: int) -> int:
    """Print anything the business sent since `since`, as a chat bubble."""
    global last_outbound_id
    data = (await http.get(f"{META}/_control/outbox")).json()
    for m in data["messages"][since:]:
        last_outbound_id = m["id"]
        if m["kind"] == "text":
            body = m["body"]["text"]["body"]
            for line in body.split("\n"):
                print(f"  {GREEN}business >{RESET} {line}")
        else:
            t = m["body"]["template"]
            params = [p["text"] for c in t.get("components", []) for p in c.get("parameters", [])]
            print(f"  {GREEN}business >{RESET} {YELLOW}[template {t['name']}]{RESET} params={params}")
    return data["count"]


async def outbox_count(http: httpx.AsyncClient) -> int:
    return (await http.get(f"{META}/_control/outbox")).json()["count"]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
HELP = f"""
{BOLD}Talk to it{RESET}
  just type              send that as a WhatsApp message from the customer
  /ad <text>             send it as a click-to-WhatsApp arrival (with ad referral)
  /dup                   resend the last webhook verbatim  {DIM}(proves idempotency){RESET}
  /lead                  simulate a Meta lead-ad submission
  /read | /delivered     send a delivery receipt for the last outbound message

{BOLD}Inspect{RESET}
  /state                 customer + conversation state
  /history               the full transcript from the database
  /decisions             what the AI asked for, and what was refused
  /reminders             scheduled follow-ups
  /funnel                attribution report
  /meta                  what the fake Meta received, and its verdicts
  /events                webhook_events table

{BOLD}Drive the system{RESET}
  /download              Boomshare reports a confirmed install
  /activate              Boomshare reports a confirmed activation
  /handoff               a human takes over
  /release               hand back to the AI
  /agent <text>          send a message as a human agent
  /fire                  make every pending reminder due now, and run it
  /window [hours]        age the conversation so the 24h window has closed

{BOLD}Break things on purpose{RESET}
  /unsigned              post a webhook with no signature      {DIM}(expect 403){RESET}
  /badtemplate           try to send an unapproved template    {DIM}(expect 132015){RESET}
  /notallowed            try to message a non-allowlisted number {DIM}(expect 131030){RESET}
  /checks                run the full contract self-check

  /trace on|off          toggle the pipeline trace
  /reset                 wipe the fake Meta state
  /help  /quit
"""


async def cmd_state(http: httpx.AsyncClient) -> None:
    from app.core.db import session_scope
    from app.models import Conversation, Customer

    async with session_scope() as session:
        customer = (
            await session.execute(select(Customer).where(Customer.phone == WA_ID))
        ).scalar_one_or_none()
        if customer is None:
            print(f"  {DIM}no customer yet - send a message first{RESET}")
            return
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.customer_id == customer.id)
            )
        ).scalar_one_or_none()

    print(f"  {BOLD}customer{RESET}      {customer.full_name or '?'}  {customer.phone}")
    print(f"  downloaded    {customer.downloaded_at or DIM + 'no' + RESET}")
    print(f"  activated     {customer.activated_at or DIM + 'no' + RESET}")
    print(f"  opted out     {customer.opted_out_at or DIM + 'no' + RESET}")
    if conversation is not None:
        print(f"  {BOLD}conversation{RESET}  {str(conversation.id)[:8]}")
        print(f"  stage         {CYAN}{conversation.sales_stage}{RESET}")
        print(f"  handled by    {conversation.handling_mode}")
        print(f"  last intent   {conversation.last_intent}")
        print(f"  notes         {conversation.context_notes or '{}'}")


async def cmd_history() -> None:
    from app.core.db import session_scope
    from app.models import Message

    async with session_scope() as session:
        rows = list(
            (await session.execute(select(Message).order_by(Message.created_at))).scalars()
        )
    if not rows:
        print(f"  {DIM}no messages yet{RESET}")
    for m in rows:
        who = f"{CYAN}customer >{RESET}" if m.direction == "inbound" else f"{GREEN}business >{RESET}"
        tag = ""
        if m.message_type == "template":
            tag = f" {YELLOW}[template]{RESET}"
        elif m.direction == "outbound" and not m.ai_generated:
            tag = f" {YELLOW}[human]{RESET}"
        if m.status == "failed":
            tag += f" {RED}[failed]{RESET}"
        print(f"  {who}{tag} {m.content}")


async def cmd_decisions() -> None:
    from app.core.db import session_scope
    from app.models import AiDecisionLog

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(select(AiDecisionLog).order_by(AiDecisionLog.created_at))
            ).scalars()
        )
    if not rows:
        print(f"  {DIM}no AI decisions yet{RESET}")
    for d in rows:
        print(f"  {BOLD}{d.intent}{RESET}  suggested={d.suggested_stage}  applied={d.applied_stage}")
        print(f"    requested {(d.requested_actions or {}).get('actions')}")
        print(f"    executed  {(d.executed_actions or {}).get('actions')}")
        if d.rejected_reasons:
            for k, v in d.rejected_reasons.items():
                print(f"    {RED}refused{RESET}   {k}: {v}")
        print(f"    {DIM}{d.prompt_tokens}+{d.completion_tokens} tokens, {d.latency_ms}ms{RESET}")


async def cmd_reminders() -> None:
    from app.core.db import session_scope
    from app.models import Reminder

    async with session_scope() as session:
        rows = list((await session.execute(select(Reminder))).scalars())
    if not rows:
        print(f"  {DIM}no reminders{RESET}")
    for r in rows:
        colour = GREEN if r.status == "pending" else DIM
        print(f"  {colour}{r.status:<10}{RESET} {r.kind:<15} due {r.due_at}  {r.resolution or ''}")


async def cmd_events() -> None:
    from app.core.db import session_scope
    from app.models import WebhookEvent

    async with session_scope() as session:
        rows = list((await session.execute(select(WebhookEvent))).scalars())
    for e in rows:
        colour = GREEN if e.status == "processed" else (RED if e.status == "failed" else YELLOW)
        print(f"  {colour}{e.status:<11}{RESET} {e.event_type:<18} attempts={e.attempts} {e.event_key}")
        if e.last_error:
            print(f"      {RED}{e.last_error[:100]}{RESET}")


async def cmd_meta(http: httpx.AsyncClient) -> None:
    state = (await http.get(f"{META}/_control/state")).json()
    print(f"  {BOLD}fake Meta{RESET}")
    print(f"  phone_number_id  {state['phone_number_id']}")
    print(f"  allowed to       {', '.join(state['allowed_recipients'])}")
    for name, t in state["templates"].items():
        colour = GREEN if t["status"] == "APPROVED" else RED
        print(f"  template         {name} ({t['language']}, {t['params']} param) {colour}{t['status']}{RESET}")
    for wa, hours in state["window_open_for"].items():
        shut = " (CLOSED)" if hours > 24 else ""
        print(f"  window           {wa}: last inbound {hours}h ago{shut}")
    print(f"\n  {BOLD}recent requests{RESET}")
    for entry in (await http.get(f"{META}/_control/log")).json()["entries"][-12:]:
        mark = f"{GREEN}ACCEPT{RESET}" if entry["ok"] else f"{RED}REJECT{RESET}"
        print(f"  {mark} {entry['kind']:<6} {entry['detail']}")


async def cmd_funnel(http: httpx.AsyncClient) -> None:
    rows = (await http.get(f"{API}/admin/reports/funnel", headers=AUTH)).json()
    if not rows:
        print(f"  {DIM}no leads yet{RESET}")
    for r in rows:
        print(
            f"  {r['campaign_name'] or DIM + 'unattributed' + RESET}: "
            f"leads={r['leads']} conversations={r['conversations']} "
            f"links={r['links_sent']} downloads={r['downloads']} activations={r['activations']}"
        )


async def _token(http: httpx.AsyncClient) -> str | None:
    from app.core.db import session_scope
    from app.models import DownloadLink

    async with session_scope() as session:
        link = (await session.execute(select(DownloadLink))).scalars().first()
    return link.token if link else None


async def cmd_install(http: httpx.AsyncClient, kind: str) -> None:
    token = await _token(http)
    if token is None:
        print(f"  {DIM}no download link sent yet - ask for the link first{RESET}")
        return
    r = await http.post(
        f"{API}/internal/events/{kind}",
        json={"token": token, "platform": "windows", "app_version": "1.4.2"},
        headers=AUTH,
    )
    body = r.json()
    mark = f"{GREEN}applied{RESET}" if body.get("applied") else f"{DIM}already recorded{RESET}"
    print(f"  {kind} event -> {mark}")


async def _conversation_id() -> str | None:
    from app.core.db import session_scope
    from app.models import Conversation

    async with session_scope() as session:
        c = (await session.execute(select(Conversation))).scalars().first()
    return str(c.id) if c else None


async def cmd_handoff(http: httpx.AsyncClient, take: bool) -> None:
    cid = await _conversation_id()
    if cid is None:
        print(f"  {DIM}no conversation yet{RESET}")
        return
    if take:
        r = await http.post(
            f"{API}/admin/conversations/{cid}/handoff",
            json={"reason": "sandbox takeover", "agent": "you@boomshare.ai"},
            headers=AUTH,
        )
        print(f"  handling_mode -> {YELLOW}{r.json()['handling_mode']}{RESET}  (the AI will stay silent)")
    else:
        r = await http.post(f"{API}/admin/conversations/{cid}/release", json={}, headers=AUTH)
        print(f"  handling_mode -> {GREEN}{r.json()['handling_mode']}{RESET}")


async def cmd_agent(http: httpx.AsyncClient, text: str) -> None:
    cid = await _conversation_id()
    if cid is None:
        print(f"  {DIM}no conversation yet{RESET}")
        return
    before = await outbox_count(http)
    r = await http.post(
        f"{API}/admin/conversations/{cid}/messages",
        json={"body": text, "agent": "you@boomshare.ai"},
        headers=AUTH,
    )
    if r.status_code != 200:
        print(f"  {RED}refused{RESET} {r.status_code}: {r.json().get('detail')}")
        return
    await show_new_messages(http, before)


async def cmd_fire(http: httpx.AsyncClient) -> None:
    from datetime import timedelta

    from app.core.clock import utcnow
    from app.core.db import session_scope
    from app.models import Reminder
    from app.worker.runner import sweep_due_reminders

    async with session_scope() as session:
        rows = list(
            (await session.execute(select(Reminder).where(Reminder.status == "pending"))).scalars()
        )
        if not rows:
            print(f"  {DIM}no pending reminders{RESET}")
            return
        for r in rows:
            r.due_at = utcnow() - timedelta(minutes=1)

    before = await outbox_count(http)
    queued = await sweep_due_reminders()
    print(f"  {queued} reminder(s) queued")
    await asyncio.sleep(1.5)
    await wait_for_worker()
    if await outbox_count(http) == before:
        print(f"  {DIM}nothing sent - check /reminders for why it was cancelled{RESET}")
    else:
        await show_new_messages(http, before)


async def cmd_window(http: httpx.AsyncClient, hours: float) -> None:
    from datetime import timedelta

    from app.core.clock import utcnow
    from app.core.db import session_scope
    from app.models import Conversation

    async with session_scope() as session:
        for c in (await session.execute(select(Conversation))).scalars():
            c.last_inbound_at = utcnow() - timedelta(hours=hours)
    await http.post(f"{META}/_control/age-window", json={"wa_id": WA_ID, "hours": hours})
    print(f"  last inbound moved to {hours}h ago - free-form text is now "
          f"{RED + 'blocked' + RESET if hours >= 24 else GREEN + 'allowed' + RESET}")


async def cmd_checks(http: httpx.AsyncClient) -> None:
    """Prove the request contract against the fake Meta, rule by rule."""
    print(f"\n  {BOLD}Contract checks against the Meta API rules{RESET}\n")
    results: list[tuple[str, bool, str]] = []

    async def call(payload: dict, headers: dict | None = None):
        return await http.post(
            f"{META}/v26.0/111222333/messages",
            json=payload,
            headers=headers if headers is not None else {"Authorization": "Bearer sandbox-access-token"},
        )

    def code_of(r: httpx.Response) -> int | None:
        try:
            return r.json().get("error", {}).get("code")
        except Exception:  # noqa: BLE001
            return None

    r = await call({"messaging_product": "whatsapp", "to": WA_ID, "type": "text",
                    "text": {"body": "hi"}}, headers={})
    results.append(("missing Bearer token is refused", code_of(r) == 190, f"code={code_of(r)}"))

    r = await call({"messaging_product": "sms", "to": WA_ID, "type": "text", "text": {"body": "x"}})
    results.append(("messaging_product must be whatsapp", code_of(r) == 100, f"code={code_of(r)}"))

    r = await call({"messaging_product": "whatsapp", "to": "not-a-number", "type": "text",
                    "text": {"body": "x"}})
    results.append(("'to' must be E.164 digits", code_of(r) == 100, f"code={code_of(r)}"))

    r = await call({"messaging_product": "whatsapp", "to": "440000000000", "type": "text",
                    "text": {"body": "x"}})
    results.append(("non-allowlisted recipient is refused", code_of(r) == 131030, f"code={code_of(r)}"))

    r = await call({"messaging_product": "whatsapp", "to": WA_ID, "type": "template",
                    "template": {"name": "does_not_exist", "language": {"code": "en"},
                                 "components": [{"type": "body", "parameters": [{"type": "text", "text": "x"}]}]}})
    results.append(("unknown template is refused", code_of(r) == 132001, f"code={code_of(r)}"))

    r = await call({"messaging_product": "whatsapp", "to": WA_ID, "type": "template",
                    "template": {"name": "boomshare_followup", "language": {"code": "en"},
                                 "components": [{"type": "body", "parameters": [
                                     {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]}})
    results.append(("wrong parameter count is refused", code_of(r) == 132000, f"code={code_of(r)}"))

    await http.post(f"{META}/_control/templates",
                    json={"name": "pending_tpl", "language": "en", "params": 1, "status": "PENDING"})
    r = await call({"messaging_product": "whatsapp", "to": WA_ID, "type": "template",
                    "template": {"name": "pending_tpl", "language": {"code": "en"},
                                 "components": [{"type": "body", "parameters": [{"type": "text", "text": "x"}]}]}})
    results.append(("unapproved template is refused", code_of(r) == 132015, f"code={code_of(r)}"))
    await http.post(f"{META}/_control/templates", json={"name": "pending_tpl", "delete": True})

    await http.post(f"{META}/_control/age-window", json={"wa_id": "447700900123", "hours": 30})
    r = await call({"messaging_product": "whatsapp", "to": "447700900123", "type": "text",
                    "text": {"body": "hello again"}})
    results.append(("free-form outside 24h is refused", code_of(r) == 131047, f"code={code_of(r)}"))

    await http.post(f"{META}/_control/inbound", json={"wa_id": "447700900123"})
    r = await call({"messaging_product": "whatsapp", "to": "447700900123", "type": "text",
                    "text": {"body": "hello again"}})
    results.append(("free-form inside 24h is accepted", r.status_code == 200, f"http={r.status_code}"))

    r = await call({"messaging_product": "whatsapp", "to": WA_ID, "type": "template",
                    "template": {"name": "boomshare_lead_intro", "language": {"code": "en"},
                                 "components": [{"type": "body", "parameters": [{"type": "text", "text": "Priya"}]}]}})
    results.append(("a correct template is accepted", r.status_code == 200, f"http={r.status_code}"))

    failed = 0
    for label, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"    [{mark}] {label}  {DIM}{detail}{RESET}")
        failed += 0 if ok else 1

    print()
    if failed:
        print(f"  {RED}{failed} check(s) failed{RESET}\n")
    else:
        print(f"  {GREEN}All contract checks passed{RESET} {DIM}- these are the rules a real "
              f"WhatsApp Business Account enforces too{RESET}\n")


async def cmd_break(http: httpx.AsyncClient, what: str) -> None:
    if what == "unsigned":
        r = await http.post(f"{API}/webhooks/meta", json=inbound_payload("sneaky"))
        colour = GREEN if r.status_code == 403 else RED
        print(f"  unsigned webhook -> {colour}{r.status_code}{RESET} {r.json().get('detail', '')}")

    elif what == "badtemplate":
        await http.post(f"{META}/_control/templates",
                        json={"name": "boomshare_followup", "language": "en", "params": 1,
                              "status": "REJECTED"})
        print(f"  marked boomshare_followup as {RED}REJECTED{RESET} in the fake Meta")
        print(f"  {DIM}now run /window 30 then /fire - the follow-up send will fail{RESET}")

    elif what == "notallowed":
        r = await http.post(
            f"{META}/v26.0/111222333/messages",
            json={"messaging_product": "whatsapp", "to": "440000000000", "type": "text",
                  "text": {"body": "hi"}},
            headers={"Authorization": "Bearer sandbox-access-token"},
        )
        err = r.json().get("error", {})
        print(f"  -> {RED}{r.status_code}{RESET} code={err.get('code')} {err.get('message')}")
        print(f"  {DIM}{(err.get('error_data') or {}).get('details', '')}{RESET}")


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
async def repl(http: httpx.AsyncClient) -> None:
    print(HELP)
    loop = asyncio.get_running_loop()

    while True:
        try:
            raw = await loop.run_in_executor(None, lambda: input(f"\n{BOLD}{CYAN}customer >{RESET} "))
        except (EOFError, KeyboardInterrupt):
            print()
            return

        line = raw.strip()
        if not line:
            continue

        if line in {"/quit", "/exit", "/q"}:
            return
        if line == "/help":
            print(HELP)
            continue

        try:
            if line.startswith("/trace"):
                tracing.enable(line.endswith("on"))
                print(f"  trace {'on' if tracing.enabled() else 'off'}")
            elif line == "/state":
                await cmd_state(http)
            elif line == "/history":
                await cmd_history()
            elif line == "/decisions":
                await cmd_decisions()
            elif line == "/reminders":
                await cmd_reminders()
            elif line == "/events":
                await cmd_events()
            elif line == "/meta":
                await cmd_meta(http)
            elif line == "/funnel":
                await cmd_funnel(http)
            elif line == "/download":
                await cmd_install(http, "download")
            elif line == "/activate":
                await cmd_install(http, "activation")
            elif line == "/handoff":
                await cmd_handoff(http, True)
            elif line == "/release":
                await cmd_handoff(http, False)
            elif line.startswith("/agent "):
                await cmd_agent(http, line[7:].strip())
            elif line == "/fire":
                await cmd_fire(http)
            elif line.startswith("/window"):
                parts = line.split()
                await cmd_window(http, float(parts[1]) if len(parts) > 1 else 30.0)
            elif line == "/checks":
                await cmd_checks(http)
            elif line in {"/unsigned", "/badtemplate", "/notallowed"}:
                await cmd_break(http, line.lstrip("/"))
            elif line == "/reset":
                await http.post(f"{META}/_control/reset")
                print("  fake Meta reset")
            elif line in {"/read", "/delivered"}:
                if last_outbound_id is None:
                    print(f"  {DIM}nothing sent yet{RESET}")
                else:
                    await post_webhook(http, status_payload(last_outbound_id, line.lstrip("/")))
                    await wait_for_worker()
                    print(f"  receipt applied to {last_outbound_id}")
            elif line == "/lead":
                before = await outbox_count(http)
                r = await post_webhook(http, LEADGEN_PAYLOAD)
                print(f"  {DIM}webhook -> {r.status_code} {r.text}{RESET}")
                await wait_for_worker()
                await show_new_messages(http, before)
            elif line == "/dup":
                if last_webhook is None:
                    print(f"  {DIM}nothing to resend{RESET}")
                else:
                    before = await outbox_count(http)
                    r = await post_webhook(http, last_webhook)
                    print(f"  {DIM}webhook -> {r.status_code} {r.text}{RESET}")
                    await wait_for_worker()
                    after = await show_new_messages(http, before)
                    if after == before:
                        print(f"  {GREEN}no duplicate reply{RESET} - idempotency held")
            else:
                referral = None
                text = line
                if line.startswith("/ad"):
                    referral = CTWA_REFERRAL
                    text = line[3:].strip() or "I want to know more"
                    print(f"  {DIM}(arriving from ad {CTWA_REFERRAL['source_id']}){RESET}")
                    print(f"  {CYAN}customer >{RESET} {text}")

                # The real Meta knows when the customer messaged; tell the fake one.
                await http.post(f"{META}/_control/inbound", json={"wa_id": WA_ID})

                before = await outbox_count(http)
                r = await post_webhook(http, inbound_payload(text, referral=referral))
                if r.status_code != 200:
                    print(f"  {RED}webhook rejected {r.status_code}{RESET} {r.text}")
                    continue
                await wait_for_worker()
                after = await show_new_messages(http, before)
                if after == before:
                    print(f"  {DIM}(no reply sent - try /decisions or /events to see why){RESET}")

        except Exception as exc:  # noqa: BLE001 - a sandbox must not die on a typo
            print(f"  {RED}error:{RESET} {exc}")


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
async def main() -> int:
    import fakeredis.aioredis

    import scripts.fake_meta as fake_meta
    from app.core import redis as redis_helper
    from app.core.db import dispose_engine
    from app.integrations.openai.client import set_sales_model
    from app.main import create_app
    from app.worker.runner import consume, schedule

    if not ARGS.postgres:
        redis_helper.set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))

    if not ARGS.real_ai:
        set_sales_model(CannedSalesModel())

    meta_server = uvicorn.Server(
        uvicorn.Config(fake_meta.app, host="127.0.0.1", port=ARGS.meta_port, log_level="error")
    )
    api_server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=ARGS.port, log_level="error")
    )
    tasks = [asyncio.create_task(meta_server.serve()), asyncio.create_task(api_server.serve())]

    for _ in range(120):
        await asyncio.sleep(0.05)
        if meta_server.started and api_server.started:
            break
    else:
        print(f"{RED}servers did not start{RESET}")
        return 1

    stop = asyncio.Event()
    workers = [asyncio.create_task(consume(stop)), asyncio.create_task(schedule(stop))]

    print(f"\n{BOLD}Boomshare sandbox{RESET}")
    print(f"  {DIM}backend    {API}{RESET}")
    print(f"  {DIM}fake Meta  {META}   (enforces Meta's real rules){RESET}")
    print(f"  {DIM}database   {'PostgreSQL from .env' if ARGS.postgres else DB_PATH}{RESET}")
    print(f"  {DIM}AI         {'real OpenAI' if ARGS.real_ai else 'canned (use --real-ai for the real model)'}{RESET}")
    print(f"  {DIM}you are    {NAME} <{WA_ID}>{RESET}")

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            await repl(http)
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
        meta_server.should_exit = api_server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis_helper.close_redis()
        await dispose_engine()

    return 0


if __name__ == "__main__":
    alembic_config = Config(str(ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(alembic_config, "head")

    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 0
    if not ARGS.postgres:
        DB_PATH.unlink(missing_ok=True)
    sys.exit(code)
