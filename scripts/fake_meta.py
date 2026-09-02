#!/usr/bin/env python
"""A local stand-in for the Meta Graph API.

This is **not** a stub that says yes to everything. It enforces the same rules
the real Cloud API enforces, and returns Meta's real error codes when you break
them. The point is that a request which succeeds here will also succeed against
a real WhatsApp Business Account - and a mistake that would fail on day one
with a live account fails here instead, on your laptop, in a second.

Rules it enforces (each one is a real failure people hit):

  190     missing or malformed Bearer token
  100     wrong phone number id, or a malformed request body
  131030  recipient is not in the allowed list       <- test numbers only reach 5
  131047  free-form message outside the 24h window   <- the single most common one
  132001  template name does not exist
  132000  wrong number of template parameters
  132015  template exists but is not APPROVED
  131009  parameter value is not valid (e.g. body over 4096 chars)

Run standalone:

    python scripts/fake_meta.py                 # serves on http://127.0.0.1:8090

Then point the backend at it - no code change, it is already a setting:

    META_GRAPH_BASE_URL=http://127.0.0.1:8090

`scripts/sandbox.py` starts this for you.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, FastAPI, Header, Path as PathParam, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from scripts import _console  # noqa: E402

_console.setup()

# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
#: Every number this fake WABA owns, primary first - mirroring the
#: application's own WHATSAPP_PHONE_NUMBER_IDS convention, so the sandbox can
#: show a reply leaving from the number the customer wrote to.
PHONE_NUMBER_IDS = ("111222333", "444555666")
PHONE_NUMBER_ID = PHONE_NUMBER_IDS[0]
BUSINESS_NUMBER = "15550001111"

#: 24 hours, in seconds. The customer service window.
WINDOW_SECONDS = 24 * 3600

#: Mirrors the real constraint on a Meta *test* number: it can only message
#: numbers you have explicitly added and verified. Production numbers have no
#: such list, which is why this is configurable.
ALLOWED_RECIPIENTS: set[str] = set()
ENFORCE_ALLOWLIST = True


class Template:
    def __init__(self, name: str, language: str, params: int, status: str = "APPROVED"):
        self.name = name
        self.language = language
        self.params = params
        self.status = status


TEMPLATES: dict[str, Template] = {}

#: wa_id -> unix seconds of the customer's last inbound message.
LAST_INBOUND: dict[str, float] = {}

#: Everything we "sent", newest last.
OUTBOX: list[dict[str, Any]] = []

#: Every request received, with the verdict. Powers `/_control/log`.
REQUEST_LOG: list[dict[str, Any]] = []

LEADS: dict[str, dict[str, Any]] = {}
ADS: dict[str, dict[str, Any]] = {}

_counter = 0


def _reset_defaults() -> None:
    """The state a fresh Meta test account would be in, roughly."""
    global _counter
    _counter = 0
    OUTBOX.clear()
    REQUEST_LOG.clear()
    LAST_INBOUND.clear()
    ALLOWED_RECIPIENTS.clear()
    ALLOWED_RECIPIENTS.update({"919876543210", "919000011111", "447700900123"})

    TEMPLATES.clear()
    # Both templates the application needs, each taking one body parameter.
    TEMPLATES["boomshare_lead_intro"] = Template("boomshare_lead_intro", "en", 1)
    TEMPLATES["boomshare_followup"] = Template("boomshare_followup", "en", 1)

    LEADS.clear()
    LEADS["LEAD-SANDBOX-1"] = {
        "id": "LEAD-SANDBOX-1",
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
    }

    ADS.clear()
    ADS["AD-CTWA-42"] = {
        "id": "AD-CTWA-42",
        "name": "CTWA - Stop writing long explanations",
        "adset_id": "ADSET-9",
        "adset": {"name": "India / Support teams"},
        "campaign_id": "CAMP-CTWA",
        "campaign": {"name": "Boomshare Q1 Click-to-WhatsApp"},
    }
    ADS["AD-LEADFORM-9"] = {
        "id": "AD-LEADFORM-9",
        "name": "Boomshare Lead Ad - Managers",
        "adset_id": "ADSET-4",
        "adset": {"name": "India / Team leads"},
        "campaign_id": "CAMP-Q1",
        "campaign": {"name": "Boomshare Q1 Lead Gen"},
    }


_reset_defaults()


# --------------------------------------------------------------------------- #
# Meta-shaped errors
# --------------------------------------------------------------------------- #
def meta_error(
    status: int,
    code: int,
    message: str,
    *,
    details: str | None = None,
    subcode: int | None = None,
) -> JSONResponse:
    """The exact envelope the Graph API returns on failure."""
    error: dict[str, Any] = {
        "message": message,
        "type": "OAuthException" if code == 190 else "GraphMethodException",
        "code": code,
        "fbtrace_id": f"Afake{int(time.time()) % 100000}",
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    if details:
        error["error_data"] = {"messaging_product": "whatsapp", "details": details}
    return JSONResponse(status_code=status, content={"error": error})


def _log(kind: str, ok: bool, detail: str, body: Any = None) -> None:
    REQUEST_LOG.append(
        {"at": time.time(), "kind": kind, "ok": ok, "detail": detail, "body": body}
    )
    mark = (
        f"{_console.GREEN}ACCEPT{_console.RESET}"
        if ok
        else f"{_console.RED}REJECT{_console.RESET}"
    )
    print(f"  [fake-meta] {mark} {kind}: {detail}", flush=True)


def _check_auth(authorization: str | None) -> JSONResponse | None:
    if not authorization or not authorization.startswith("Bearer "):
        _log("auth", False, "missing or malformed Authorization header")
        return meta_error(
            401, 190, "An access token is required to request this resource.", subcode=1
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        _log("auth", False, "empty bearer token")
        return meta_error(401, 190, "Invalid OAuth access token.", subcode=1)
    return None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
router = APIRouter()


@router.post("/{version}/{phone_number_id}/messages")
async def send_message(
    request: Request,
    version: str = PathParam(...),
    phone_number_id: str = PathParam(...),
    authorization: str | None = Header(default=None),
):
    """The WhatsApp Cloud API send endpoint, with real validation."""
    global _counter

    if (denied := _check_auth(authorization)) is not None:
        return denied

    if not re.fullmatch(r"v\d+\.\d+", version):
        _log("send", False, f"bad graph version {version!r}")
        return meta_error(400, 100, f"Unknown path components: /{version}")

    if phone_number_id not in PHONE_NUMBER_IDS:
        _log("send", False, f"unknown phone_number_id {phone_number_id!r}")
        return meta_error(
            404,
            100,
            f"Unsupported get request. Object with ID '{phone_number_id}' does not exist",
            subcode=33,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        _log("send", False, "body was not valid json")
        return meta_error(400, 100, "Invalid parameter")

    if body.get("messaging_product") != "whatsapp":
        _log("send", False, "messaging_product is not 'whatsapp'")
        return meta_error(
            400, 100, "Param messaging_product must be 'whatsapp'", details="messaging_product"
        )

    # Read receipts share this endpoint.
    if body.get("status") == "read":
        if not body.get("message_id"):
            _log("read", False, "read receipt without message_id")
            return meta_error(400, 100, "Param message_id is required")
        _log("read", True, f"marked {body['message_id']} as read")
        return {"success": True}

    to = str(body.get("to") or "")
    if not to:
        _log("send", False, "missing 'to'")
        return meta_error(400, 100, "Param to is required", details="to")
    if not to.isdigit():
        _log("send", False, f"'to' is not E.164 digits: {to!r}")
        return meta_error(
            400,
            100,
            "Param to must be a valid phone number in E.164 format without '+'",
            details="to",
        )

    if ENFORCE_ALLOWLIST and to not in ALLOWED_RECIPIENTS:
        _log("send", False, f"{to} is not in the allowed recipient list")
        return meta_error(
            400,
            131030,
            "Recipient phone number not in allowed list",
            details=(
                f"Recipient phone number not in allowed list: Add {to} in the allowed list "
                "and try again."
            ),
        )

    message_type = body.get("type")

    if message_type == "text":
        return _handle_text(body, to, phone_number_id)
    if message_type == "template":
        return _handle_template(body, to, phone_number_id)

    _log("send", False, f"unsupported type {message_type!r}")
    return meta_error(400, 100, f"Param type must be one of text, template (got {message_type})")


def _accept(to: str, kind: str, body: dict[str, Any], summary: str, phone_number_id: str):
    global _counter
    _counter += 1
    message_id = f"wamid.FAKE{_counter:05d}"
    OUTBOX.append(
        {
            "id": message_id,
            "to": to,
            "kind": kind,
            "body": body,
            "from_phone_number_id": phone_number_id,
            "at": time.time(),
        }
    )
    _log("send", True, summary, body)
    return {
        "messaging_product": "whatsapp",
        "contacts": [{"input": to, "wa_id": to}],
        "messages": [{"id": message_id, "message_status": "accepted"}],
    }


def _handle_text(body: dict[str, Any], to: str, phone_number_id: str):
    text = body.get("text")
    if not isinstance(text, dict) or not text.get("body"):
        _log("send", False, "type=text but text.body is missing")
        return meta_error(400, 100, "Param text['body'] is required", details="text")

    content = str(text["body"])
    if len(content) > 4096:
        _log("send", False, f"body is {len(content)} chars, max is 4096")
        return meta_error(400, 131009, "Parameter value is not valid", details="text.body")

    # The rule that catches everyone: free-form text is only allowed inside 24
    # hours of the customer's last inbound message.
    last = LAST_INBOUND.get(to)
    if last is None or (time.time() - last) > WINDOW_SECONDS:
        age = "never messaged us" if last is None else f"{int((time.time() - last) / 3600)}h ago"
        _log("send", False, f"outside the 24h window ({age}) - a template is required")
        return meta_error(
            400,
            131047,
            "Re-engagement message",
            details=(
                "Message failed to send because more than 24 hours have passed since the "
                "customer last replied to this number."
            ),
        )

    return _accept(to, "text", body, f'text to {to}: "{content[:60]}"', phone_number_id)


def _handle_template(body: dict[str, Any], to: str, phone_number_id: str):
    template = body.get("template")
    if not isinstance(template, dict) or not template.get("name"):
        _log("send", False, "type=template but template.name is missing")
        return meta_error(400, 100, "Param template['name'] is required", details="template")

    name = str(template["name"])
    language = ((template.get("language") or {}).get("code")) or ""

    known = TEMPLATES.get(name)
    if known is None:
        _log("send", False, f"template {name!r} does not exist")
        return meta_error(
            400,
            132001,
            "Template name does not exist in the translation",
            details=(
                f"template name ({name}) does not exist in {language or 'the given language'}"
            ),
        )

    if language != known.language:
        _log("send", False, f"template {name!r} has no {language!r} translation")
        return meta_error(
            400,
            132001,
            "Template name does not exist in the translation",
            details=f"template name ({name}) does not exist in {language}",
        )

    if known.status != "APPROVED":
        _log("send", False, f"template {name!r} is {known.status}, not APPROVED")
        return meta_error(
            400,
            132015,
            "Template is paused",
            details=f"Template {name} is {known.status} and cannot be sent.",
        )

    supplied = 0
    for component in template.get("components") or []:
        if isinstance(component, dict) and component.get("type") == "body":
            supplied = len(component.get("parameters") or [])

    if supplied != known.params:
        _log("send", False, f"template {name!r} wants {known.params} params, got {supplied}")
        return meta_error(
            400,
            132000,
            "Number of parameters does not match the expected number of params",
            details=(
                f"body: number of localizable_params ({supplied}) does not match the expected "
                f"number of params ({known.params})"
            ),
        )

    return _accept(
        to, "template", body, f"template '{name}' to {to} with {supplied} param(s)", phone_number_id
    )


@router.get("/{version}/{node_id}")
async def read_node(
    version: str = PathParam(...),
    node_id: str = PathParam(...),
    authorization: str | None = Header(default=None),
):
    """Graph node reads: a lead submission or an ad."""
    if (denied := _check_auth(authorization)) is not None:
        return denied

    if node_id in LEADS:
        _log("fetch", True, f"lead {node_id}")
        return LEADS[node_id]

    if node_id in ADS:
        _log("fetch", True, f"ad {node_id}")
        return ADS[node_id]

    _log("fetch", False, f"unknown node {node_id}")
    return meta_error(
        400,
        100,
        f"Unsupported get request. Object with ID '{node_id}' does not exist, is not "
        "supported, or you do not have permission to access it.",
        subcode=33,
    )


# --------------------------------------------------------------------------- #
# Control surface - not part of Meta, used by the sandbox
# --------------------------------------------------------------------------- #
control = APIRouter(prefix="/_control")


@control.post("/inbound")
async def note_inbound(request: Request):
    """Tell the fake Meta that a customer just messaged us.

    The real Meta tracks this itself; here the sandbox has to say so, otherwise
    the 24-hour window could never open.
    """
    body = await request.json()
    wa_id = str(body["wa_id"])
    LAST_INBOUND[wa_id] = float(body.get("at") or time.time())
    return {"ok": True, "wa_id": wa_id}


@control.post("/age-window")
async def age_window(request: Request):
    """Push a customer's last-inbound time into the past, to test the window."""
    body = await request.json()
    wa_id = str(body["wa_id"])
    hours = float(body.get("hours", 25))
    LAST_INBOUND[wa_id] = time.time() - hours * 3600
    return {"ok": True, "wa_id": wa_id, "hours_ago": hours}


@control.get("/outbox")
async def get_outbox():
    return {"count": len(OUTBOX), "messages": OUTBOX}


@control.get("/log")
async def get_log():
    return {"count": len(REQUEST_LOG), "entries": REQUEST_LOG}


@control.get("/state")
async def get_state():
    return {
        "phone_number_ids": list(PHONE_NUMBER_IDS),
        "allowlist_enforced": ENFORCE_ALLOWLIST,
        "allowed_recipients": sorted(ALLOWED_RECIPIENTS),
        "templates": {
            name: {"language": t.language, "params": t.params, "status": t.status}
            for name, t in TEMPLATES.items()
        },
        "window_open_for": {
            wa: round((time.time() - at) / 3600, 2) for wa, at in LAST_INBOUND.items()
        },
        "sent": len(OUTBOX),
    }


@control.post("/templates")
async def set_template(request: Request):
    """Add, remove or change the approval status of a template."""
    body = await request.json()
    name = str(body["name"])
    if body.get("delete"):
        TEMPLATES.pop(name, None)
        return {"ok": True, "deleted": name}
    TEMPLATES[name] = Template(
        name,
        str(body.get("language", "en")),
        int(body.get("params", 1)),
        str(body.get("status", "APPROVED")).upper(),
    )
    return {"ok": True, "template": name, "status": TEMPLATES[name].status}


@control.post("/allowlist")
async def set_allowlist(request: Request):
    global ENFORCE_ALLOWLIST
    body = await request.json()
    if "enforce" in body:
        ENFORCE_ALLOWLIST = bool(body["enforce"])
    if "add" in body:
        ALLOWED_RECIPIENTS.add(str(body["add"]))
    if "remove" in body:
        ALLOWED_RECIPIENTS.discard(str(body["remove"]))
    return {"ok": True, "enforced": ENFORCE_ALLOWLIST, "allowed": sorted(ALLOWED_RECIPIENTS)}


@control.post("/reset")
async def reset():
    _reset_defaults()
    return {"ok": True}


@control.get("/health")
async def health():
    return {"status": "ok", "service": "fake-meta"}


def create_app() -> FastAPI:
    app = FastAPI(title="Fake Meta Graph API", docs_url=None, redoc_url=None)
    app.include_router(control)
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    print("Fake Meta Graph API on http://127.0.0.1:8090")
    print(f"  phone_number_ids: {', '.join(PHONE_NUMBER_IDS)}")
    print(f"  templates       : {', '.join(TEMPLATES)}")
    print(f"  allowed to      : {', '.join(sorted(ALLOWED_RECIPIENTS))}")
    print("\nPoint the backend at it with:")
    print("  META_GRAPH_BASE_URL=http://127.0.0.1:8090\n")
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")
