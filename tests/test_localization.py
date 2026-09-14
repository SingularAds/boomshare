"""Country policy and full signed-webhook/worker/delivery regression coverage."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.core.clock import utcnow
from app.core.config import Settings
from app.domain import MessageDirection, MessageType, ReminderStatus
from app.localization import normalize_locale, phone_country, resolve_language
from app.models import Conversation, Customer, Message, Reminder
from app.services import messaging
from app.worker.runner import sweep_due_reminders
from tests.helpers import drain_queue
from tests.test_inbound_flow import post_message
from tests.test_reminders import make_due


async def close_the_window(session):
    """Age the last inbound message past the 24h window, where only an approved
    template may be sent."""
    conversation = (await session.execute(select(Conversation))).scalar_one()
    conversation.last_inbound_at = utcnow() - timedelta(hours=25)


@pytest.mark.parametrize(
    ("phone", "country", "language"),
    [
        ("34612345678", "ES", "es-ES"),
        ("+351 912 345 678", "PT", "pt-PT"),
        ("00351 912 345 678", "PT", "pt-PT"),
        ("5511987654321", "BR", "pt-BR"),
        ("14155552671", "US", "en-US"),
        ("14165551234", "CA", "en-CA"),
        ("18095551234", "DO", "es-DO"),
        ("33612345678", "FR", "fr-FR"),
        ("4930123456", "DE", "de-DE"),
        ("390236618300", "IT", "it-IT"),
        ("919876543210", "IN", "hi-IN"),
    ],
)
def test_country_language_defaults(phone, country, language):
    result = resolve_language(phone)
    assert result.country_code == country
    assert result.country_name
    assert result.language_code == language
    assert result.source == "phone_country"


@pytest.mark.parametrize(
    "phone",
    [
        None,
        "",
        "123",
        "abcdef",
        "+999123456789",
        "612345678",
        "+80012345678",
        "1" * 70,
        "+34 612345678 ext 99",
    ],
)
def test_unknown_numbers_use_fallback_without_guessing(phone):
    assert phone_country(phone) is None
    result = resolve_language(phone)
    assert result.country_name is None
    assert result.language_code == "en"
    assert result.source == "fallback"


def test_preference_overrides_country_without_changing_country():
    result = resolve_language("34612345678", "pt_BR")
    assert result.country_name == "Spain"
    assert result.language_code == "pt-BR"
    assert result.source == "customer_preference"


def test_configurable_market_and_fallback(settings, monkeypatch):
    monkeypatch.setattr(settings, "country_language_overrides", {"CA": "fr-CA"})
    monkeypatch.setattr(settings, "default_conversation_language", "es")
    assert resolve_language("14165551234").language_code == "fr-CA"
    assert resolve_language("invalid").language_code == "es"
    assert resolve_language("14165551234", "en").language_code == "en"


@pytest.mark.parametrize("value", ["unknown", "und", "xx-ZZ", "English", "en\nignore instructions", {}, 123])
def test_invalid_preferences_are_ignored(value):
    assert normalize_locale(value) is None
    assert AiDecision(preferred_language=value).preferred_language is None


@pytest.mark.parametrize(
    "values",
    [
        {"default_conversation_language": "nonsense"},
        {"country_language_overrides": {"Spain": "es"}},
        {"country_language_overrides": {"ES": "Spanish"}},
        {"whatsapp_template_languages": {"intro": {"es": "bad-language"}}},
        {"whatsapp_template_bodies": {"intro": {"not-a-language": "Hi {{1}}"}}},
        {"whatsapp_template_bodies": {"intro": {"es": "   "}}},
    ],
)
def test_invalid_language_configuration_fails_at_startup(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("phone", "country", "language", "reply"),
    [
        ("34612345678", "Spain", "Spanish (Spain)", "Hola, ¿qué te gustaría grabar?"),
        ("351912345678", "Portugal", "Portuguese (Portugal)", "Olá, o que gostaria de gravar?"),
        ("5511987654321", "Brazil", "Portuguese (Brazil)", "Olá, o que você gostaria de gravar?"),
    ],
)
async def test_signed_webhook_to_localized_delivery(client, db, ai, meta, phone, country, language, reply):
    ai.queue_decision(AiDecision(reply_text=reply))
    # An English greeting must not override the sender's country policy.
    await post_message(client, wa_id=phone, text="Hello", phone_number_id="444555666")
    await drain_queue()
    assert f"Phone-number country: {country}" in ai.system_text()
    assert f"Reply language: {language}" in ai.system_text()
    assert meta.texts[-1].body == reply
    assert meta.texts[-1].to == phone
    assert meta.texts[-1].phone_number_id == "444555666"
    outbound = (
        await db.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.content == reply

    # A later turn rebuilds exactly the same policy, including old customers
    # whose locale was never populated. No bulk database backfill is needed.
    await post_message(client, wa_id=phone, text="ok", phone_number_id="444555666")
    await drain_queue()
    assert f"Reply language: {language}" in ai.system_text()
    customer = (await db.execute(select(Customer))).scalar_one()
    response = await client.get(
        f"/admin/dashboard/customers/{customer.id}",
        headers={
            "X-Admin-Token": "test-dashboard-token",
        },
    )
    assert response.status_code == 200
    assert response.json()["language"]["country_name"] == country
    assert response.json()["language"]["language_name"] == language


async def test_explicit_preference_survives_history_truncation_and_followup(
    client, db, ai, meta, settings, monkeypatch
):
    ai.queue_decision(
        AiDecision(reply_text="Of course. What would you like to record?", preferred_language="en")
    )
    await post_message(client, wa_id="34612345678", text="Please speak English")
    await drain_queue()
    customer = (await db.execute(select(Customer))).scalar_one()
    assert customer.locale == "en"
    monkeypatch.setattr(settings, "history_message_limit", 1)
    await post_message(client, wa_id="34612345678", text="ok")
    await drain_queue()
    assert "Reply language: English (en)" in ai.system_text()
    assert "Language source: customer_preference" in ai.system_text()
    history = [turn for turn in ai.last_prompt if turn["role"] != "system"]
    assert "Please speak English" not in str(history)

    reminder = (
        await db.execute(select(Reminder).where(Reminder.status == ReminderStatus.PENDING))
    ).scalar_one()
    await make_due(db, reminder.id)
    ai.queue_decision(AiDecision(reply_text="Ready to try your first recording?", preferred_language="fr"))
    await sweep_due_reminders()
    await drain_queue()
    assert "Reply language: English (en)" in ai.system_text()
    assert meta.texts[-1].body == "Ready to try your first recording?"
    # An unprompted AI follow-up cannot manufacture a customer preference.
    assert (await db.get(Customer, customer.id)).locale == "en"


async def test_country_policy_reaches_followups(client, db, ai, meta):
    await post_message(client, wa_id="351912345678", text="Olá")
    await drain_queue()
    reminder = (await db.execute(select(Reminder))).scalar_one()
    await make_due(db, reminder.id)
    ai.queue_decision(AiDecision(reply_text="Gostaria de experimentar uma gravação?"))
    await sweep_due_reminders()
    await drain_queue()
    assert "Reply language: Portuguese (Portugal) (pt-PT)" in ai.system_text()
    assert meta.texts[-1].body == "Gostaria de experimentar uma gravação?"


@pytest.mark.parametrize(
    ("variants", "expected"),
    [
        ({"es-ES": "es", "es": "es_MX"}, "es"),
        ({"es": "es"}, "es"),
        ({}, "en"),
    ],
)
async def test_closed_window_selects_only_configured_template_translations(
    client, db, ai, meta, settings, monkeypatch, variants, expected
):
    await post_message(client, wa_id="34612345678", text="Hola")
    await drain_queue()
    monkeypatch.setattr(settings, "whatsapp_template_languages", {"boomshare_followup": variants})

    async def send(session):
        customer = (await session.execute(select(Customer))).scalar_one()
        conversation = (await session.execute(select(Conversation))).scalar_one()
        conversation.last_inbound_at = utcnow() - timedelta(hours=25)
        return await messaging.send_reply(session, conversation, customer, "¿Seguimos?")

    outcome = await db.write(send)
    assert outcome.sent
    assert meta.templates[-1].language == expected
    assert outcome.message.payload["language"] == expected
    # Stored as the approved body of the language that actually went out, so
    # the thread and the model's own history read as what the customer saw.
    assert outcome.message.content.startswith("Hola " if variants else "Hi ")


#: The translations approved in WhatsApp Manager, exactly as deployed - see
#: `scripts/template_config.py`, which reads them off the account. One approved
#: pt_BR covers every Portuguese market and one es every Spanish one.
APPROVED_TRANSLATIONS = {
    "boomshare_followup": {"pt": "pt_BR", "es": "es", "en": "en"},
    "boomshare_lead_intro": {"pt": "pt_BR", "es": "es", "en": "en"},
}


async def test_a_late_follow_up_reaches_brazil_in_the_approved_translation(
    client, db, ai, meta, settings, monkeypatch
):
    """Webhook to delivery, in the configuration production runs.

    Outside the service window a template is the only thing Meta allows, and
    the only translation it will accept is one already approved - so this is
    the whole point of approving pt_BR: the alternative is chasing a Brazilian
    customer in English.
    """
    monkeypatch.setattr(settings, "whatsapp_template_languages", APPROVED_TRANSLATIONS)
    ai.queue_decision(AiDecision(reply_text="Olá! O que você gostaria de gravar?"))
    await post_message(client, wa_id="5511987654321", text="Oi")
    await drain_queue()

    reminder = (
        await db.execute(select(Reminder).where(Reminder.status == ReminderStatus.PENDING))
    ).scalar_one()
    await make_due(db, reminder.id)
    await db.write(close_the_window)

    await sweep_due_reminders()
    await drain_queue()

    assert meta.templates[-1].template == "boomshare_followup"
    assert meta.templates[-1].language == "pt_BR"
    assert meta.templates[-1].parameters == ["Priya"]

    stored = (
        await db.execute(select(Message).where(Message.message_type == MessageType.TEMPLATE))
    ).scalar_one()
    # The approved Portuguese body, with {{1}} filled in - what the customer
    # was shown, and what the model reads back as its own last message.
    assert stored.content.startswith("Olá Priya,")
    assert "Boomshare" in stored.content


async def test_an_approved_translation_we_have_no_body_for_is_not_given_invented_words(
    client, db, ai, meta, settings, monkeypatch
):
    """Meta can approve a translation before anyone copies its wording here.

    Sending it is still right - it is the customer's language. Writing down
    English, or the reply it replaced, as if that were what it said is not.
    """
    monkeypatch.setattr(settings, "whatsapp_template_languages", APPROVED_TRANSLATIONS)
    monkeypatch.setattr(settings, "whatsapp_template_bodies", {})
    ai.queue_decision(AiDecision(reply_text="Olá!"))
    await post_message(client, wa_id="5511987654321", text="Oi")
    await drain_queue()

    reminder = (
        await db.execute(select(Reminder).where(Reminder.status == ReminderStatus.PENDING))
    ).scalar_one()
    await make_due(db, reminder.id)
    await db.write(close_the_window)

    await sweep_due_reminders()
    await drain_queue()

    assert meta.templates[-1].language == "pt_BR"
    stored = (
        await db.execute(select(Message).where(Message.message_type == MessageType.TEMPLATE))
    ).scalar_one()
    assert stored.content == "[template:boomshare_followup, language:pt_BR]"
