"""Read the approved templates out of Meta and print the config for them.

`WHATSAPP_TEMPLATE_LANGUAGES` and `WHATSAPP_TEMPLATE_BODIES` have to agree with
WhatsApp Manager exactly, and getting either wrong fails quietly in a different
way. A language code we send that Meta has not approved is error 132001 and the
follow-up never arrives. A body that drifts from the approved wording is worse
than useless: it is stored as the message we sent, so the dashboard shows words
the customer never saw and the model answers for them on the next turn.

Neither is something to transcribe by hand. Run this instead, after approving a
translation or editing one, and paste what it prints:

    python scripts/template_config.py --waba-id 123456789012345

The WhatsApp Business Account ID is in WhatsApp Manager -> API Setup, directly
under the phone number ID (it is NOT the phone number ID). It can also come
from WHATSAPP_BUSINESS_ACCOUNT_ID in the environment.

Read-only: it lists templates and sends nothing. Only APPROVED languages are
emitted, so a translation still in review cannot reach the configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.localization import normalize_locale  # noqa: E402
from scripts import _console  # noqa: E402
from scripts._console import BOLD, DIM, GREEN, RED, RESET, YELLOW  # noqa: E402

_console.setup()

#: Meta reviews each language of a template separately; the rest are drafts,
#: rejected, or still in review, and sending any of them is error 132001.
APPROVED = "APPROVED"


async def fetch_templates(waba_id: str) -> list[dict]:
    """Every template on the account, following Meta's paging to the end."""
    settings = get_settings()
    token = settings.meta_access_token.get_secret_value()
    if not token:
        raise SystemExit("META_ACCESS_TOKEN is not set")

    url = f"{settings.graph_url}/{waba_id}/message_templates"
    params: dict[str, str] = {"fields": "name,language,status,components", "limit": "100"}
    templates: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            response = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            body = response.json()
            if response.status_code != 200:
                error = body.get("error", {})
                raise SystemExit(
                    f"Graph API {response.status_code}: {error.get('message', body)}\n"
                    "Check the WABA id, and that the token carries "
                    "whatsapp_business_management."
                )
            templates.extend(body.get("data", []))
            url = body.get("paging", {}).get("next", "")
            params = {}  # `next` already carries them

    return templates


def body_text(template: dict) -> str | None:
    for component in template.get("components", []):
        if component.get("type") == "BODY":
            return component.get("text")
    return None


def conversation_key(meta_language: str, siblings: set[str]) -> str:
    """The customer language this translation should serve.

    Template selection tries the exact locale first and then the base language,
    so keying an only-child by its base language is what makes one approved
    `pt_BR` serve Portugal as well as Brazil. Where a template has two variants
    of the same language, neither can stand in for the other and both are keyed
    exactly.
    """
    locale = normalize_locale(meta_language) or meta_language
    base = locale.split("-")[0]
    others = {
        (normalize_locale(other) or other).split("-")[0]
        for other in siblings
        if other != meta_language
    }
    return base if base not in others else locale


def ours() -> set[str]:
    """The templates this deployment can actually send.

    An account collects others - Meta seeds every new one with `hello_world`,
    and marketing may add their own. They are nothing to do with us, and
    carrying them in our configuration would only invite someone to wire one up
    by editing an env var instead of the code that decides what we send.
    """
    settings = get_settings()
    return {settings.whatsapp_lead_template_name, settings.whatsapp_followup_template_name}


def build(templates: list[dict], names: set[str] | None) -> tuple[dict, dict, list[str]]:
    languages: dict[str, dict[str, str]] = {}
    bodies: dict[str, dict[str, str]] = {}
    skipped: list[str] = []

    if names is not None:
        templates = [t for t in templates if t.get("name") in names]

    approved: dict[str, set[str]] = {}
    for template in templates:
        if template.get("status") != APPROVED:
            skipped.append(
                f"{template.get('name')} / {template.get('language')} "
                f"-> {template.get('status')}"
            )
            continue
        approved.setdefault(template["name"], set()).add(template["language"])

    for template in templates:
        name, language = template.get("name"), template.get("language")
        if language not in approved.get(name, set()):
            continue

        languages.setdefault(name, {})[conversation_key(language, approved[name])] = language

        text = body_text(template)
        if text is None:
            skipped.append(f"{name} / {language} -> no BODY component")
            continue
        bodies.setdefault(name, {})[language] = text

    return languages, bodies, skipped


def report(languages: dict, bodies: dict, skipped: list[str]) -> None:
    settings = get_settings()

    print(f"\n{BOLD}Approved{RESET}")
    for name in sorted(languages):
        for key, meta_language in sorted(languages[name].items()):
            text = bodies.get(name, {}).get(meta_language, "")
            print(f"  {GREEN}OK{RESET} {name} / {meta_language}  {DIM}(serves {key}){RESET}")
            print(f"     {DIM}{text}{RESET}")

    if skipped:
        print(f"\n{BOLD}Not usable yet{RESET}")
        for line in skipped:
            print(f"  {YELLOW}--{RESET} {line}")

    live = settings.whatsapp_template_languages
    drifted = [
        f"{name} / {meta_language}"
        for name, variants in live.items()
        for meta_language in variants.values()
        if meta_language not in bodies.get(name, {})
    ]
    if drifted:
        print(f"\n{RED}Configured but NOT approved on this account{RESET}")
        for line in drifted:
            print(f"  {RED}!!{RESET} {line}   <- sending this is error 132001")

    print(f"\n{BOLD}Paste into .env / deploy/env.prod.yaml{RESET}\n")
    for key, value in (
        ("WHATSAPP_TEMPLATE_LANGUAGES", languages),
        ("WHATSAPP_TEMPLATE_BODIES", bodies),
    ):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        print(f"{key}='{rendered}'\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--waba-id",
        default=os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", ""),
        help="WhatsApp Business Account ID (WhatsApp Manager -> API Setup)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include every template on the account, not only the two we send",
    )
    args = parser.parse_args()
    if not args.waba_id:
        parser.error("--waba-id is required (or set WHATSAPP_BUSINESS_ACCOUNT_ID)")

    templates = asyncio.run(fetch_templates(args.waba_id))
    if not templates:
        print(f"{YELLOW}No templates on account {args.waba_id}.{RESET}")
        return 1

    report(*build(templates, None if args.all else ours()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
