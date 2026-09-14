"""Exercise signed HTTP webhooks with the real configured LLM.

Uses an isolated SQLite database and fakeredis. Meta deliveries are captured
unless --send-to explicitly allowlists one recipient. Synthetic country test
numbers are never contacted. Requires requirements-dev.txt and an OpenAI key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fakeredis.aioredis
import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import select


class DeliveryTransport(httpx.AsyncBaseTransport):
    """Real Meta serialization, with an explicit recipient allowlist at HTTP."""

    def __init__(self, allowed_recipient: str | None):
        self.allowed_recipient = allowed_recipient
        self.network = httpx.AsyncHTTPTransport()
        self.deliveries: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/messages"):
            raise RuntimeError("Live language QA only supports the messages endpoint")
        payload = json.loads(request.content)
        if payload.get("status") == "read":
            # The test generates inbound IDs, so there is no real receipt to mark.
            return httpx.Response(200, json={"success": True})
        if payload.get("type") != "text":
            raise RuntimeError("Live language QA does not send business templates")
        live = bool(self.allowed_recipient) and payload.get("to") == self.allowed_recipient
        if live:
            response = await self.network.handle_async_request(request)
            await response.aread()
        else:
            response = httpx.Response(200, json={"messages": [{"id": f"captured.{uuid.uuid4()}"}]})
        body = response.json()
        self.deliveries.append({
            "live": live,
            "text": payload["text"]["body"],
            "http_status": response.status_code,
            "provider_message_id": next(iter(body.get("messages", [])), {}).get("id"),
            "error_code": body.get("error", {}).get("code"),
        })
        return response

    async def aclose(self):
        await self.network.aclose()


class ObservedModel:
    def __init__(self, real):
        self.real = real
        self.calls: list[dict] = []

    async def decide(self, messages):
        result = await self.real.decide(messages)
        self.calls.append({
            "language_policy": next(m["content"] for m in messages if
                                    m["role"] == "system" and m["content"].startswith("# Conversation language")),
            "decision": result.decision.model_dump(mode="json"),
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": result.latency_ms,
        })
        return result


async def run(args, output: Path) -> int:
    from app.core import redis as redis_helper
    from app.core.config import get_settings
    from app.core.db import dispose_engine, session_scope
    from app.integrations.meta.client import MetaClient, set_meta_client
    from app.integrations.openai.client import OpenAiSalesModel, set_sales_model
    from app.main import create_app
    from app.models import Customer, WebhookEvent
    from tests.factories import signed, whatsapp_message_payload
    from tests.helpers import drain_queue

    settings = get_settings()
    transport = DeliveryTransport(args.send_to)
    meta_http = httpx.AsyncClient(transport=transport, timeout=25)
    set_meta_client(MetaClient(settings, client=meta_http))
    real_model = OpenAiSalesModel(settings)
    model = ObservedModel(real_model)
    set_sales_model(model)
    redis_helper.set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
            raise RuntimeError("QA HTTP server failed to start")
        await asyncio.sleep(0.05)

    cases = []
    async def turn(http, label, phone, message, expected, *, preference=None, download=False):
        calls_before = len(model.calls)
        sent_before = len(transport.deliveries)
        payload = whatsapp_message_payload(
            wa_id=phone, text=message, profile_name="Language QA",
            phone_number_id=settings.default_phone_number_id,
            message_id=f"wamid.language-qa.{uuid.uuid4()}",
        )
        body, headers = signed(payload, secret=settings.meta_app_secret.get_secret_value())
        response = await http.post("/webhooks/meta", content=body, headers=headers)
        response.raise_for_status()
        await drain_queue()
        async with session_scope() as session:
            customer = (await session.execute(select(Customer).where(Customer.phone == phone))).scalar_one()
            preference = customer.locale
        case = {
            "label": label, "input": message, "expected_language": expected,
            "saved_preference": preference,
            "calls": model.calls[calls_before:],
            "deliveries": transport.deliveries[sent_before:],
        }
        case["pipeline_passed"] = bool(case["calls"] and case["deliveries"]) and all(
            item["http_status"] == 200 for item in case["deliveries"]
        )
        case["preference_passed"] = preference is None or preference == case["saved_preference"]
        case["download_passed"] = not download or any(
            "https://" in item["text"] for item in case["deliveries"]
        )
        cases.append(case)
        (output / "live-results.json").write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(case, ensure_ascii=True), flush=True)
        if not all(case[key] for key in ("pipeline_passed", "preference_passed", "download_passed")):
            raise RuntimeError(f"Live behavior check failed for {label}")

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=90) as http:
            if not args.send_to:
                await turn(http, "Spain: English greeting", "34612345678", "Hello", "es-ES")
                await turn(http, "Spain: pricing in English", "34612345678", "How much does it cost?", "es-ES")
                await turn(http, "Portugal: greeting", "351912345678", "Hello", "pt-PT")
                await turn(http, "Portugal: download request", "351912345678", "Quero o link para Windows", "pt-PT", download=True)
                await turn(http, "Brazil: greeting", "5511987654321", "Hello", "pt-BR")
                await turn(http, "Spain: explicit English", "34612345678", "Please speak English", "en", preference="en")
                settings.history_message_limit = 1
                await turn(http, "Spain: preference beyond history", "34612345678", "Tell me more", "en")
                await turn(http, "India: default", "919876543210", "Hello", "hi-IN")
                await turn(http, "India: switch to Spanish", "919876543210", "Please speak Spanish", "es", preference="es")
                await turn(http, "India: switch to Portuguese", "919876543210", "Please speak Portuguese from Portugal", "pt-PT", preference="pt-PT")
                await turn(http, "India: switch back to English", "919876543210", "Please speak English", "en", preference="en")
            else:
                await turn(http, "Live WhatsApp: India default", args.send_to, "Hi", "hi-IN")
                await turn(http, "Live WhatsApp: Spanish request", args.send_to, "Please speak Spanish", "es", preference="es")
                await turn(http, "Live WhatsApp: Portuguese request", args.send_to, "Please speak Portuguese from Portugal", "pt-PT", preference="pt-PT")
                await turn(http, "Live WhatsApp: English request", args.send_to, "Please speak English", "en", preference="en")
            async with session_scope() as session:
                events = (await session.execute(select(WebhookEvent))).scalars().all()
                assert all(str(event.status) == "processed" for event in events)
        return 0
    finally:
        server.should_exit = True
        await serving
        await real_model.aclose()
        await meta_http.aclose()
        await redis_helper.close_redis()
        await dispose_engine()
        listener.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-to", help="Explicitly authorized E.164 test recipient; real Meta delivery")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/language-qa-20260914")
    args = parser.parse_args()
    if args.send_to and (not args.send_to.isascii() or not args.send_to.isdigit()):
        parser.error("--send-to must be international digits, without '+'")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = output / f"qa-{uuid.uuid4().hex[:8]}.db"
    os.environ.update({
        "ENVIRONMENT": "local", "DEBUG": "false", "RUN_EMBEDDED_WORKER": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "REDIS_URL": "redis://localhost:6379/15", "LOG_LEVEL": "ERROR", "LOG_JSON": "false",
    })
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.openai_api_key.get_secret_value() or not settings.default_phone_number_id:
        parser.error("Configure OPENAI_API_KEY and WHATSAPP_PHONE_NUMBER_IDS in .env")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    return asyncio.run(run(args, output))


if __name__ == "__main__":
    raise SystemExit(main())
