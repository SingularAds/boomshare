"""Phone-country defaults and explicit language preferences, resolved offline.

Phone regions describe a numbering plan, not a person's location or identity.
Country defaults use CLDR's most widely used official/de-facto language and can
be overridden for a market. No phone numbers are retained in process caches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import phonenumbers
from babel import Locale, UnknownLocaleError
from babel.languages import get_official_languages


@dataclass(frozen=True, slots=True)
class ConversationLanguage:
    country_code: str | None
    country_name: str | None
    language_code: str
    language_name: str
    source: Literal["customer_preference", "phone_country", "fallback"]


def normalize_locale(value: str | None) -> str | None:
    """Accept supported language/locale tags, never arbitrary prompt text."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,2}", value):
        return None
    if value.replace("_", "-").split("-")[0].lower() == "und":
        return None
    try:
        return str(Locale.parse(value.replace("-", "_"))).replace("_", "-")
    except (ValueError, UnknownLocaleError):
        return None


def phone_country(phone: str | None) -> str | None:
    """Resolve a full international number, including shared calling codes.

    WhatsApp supplies E.164 digits without '+'. Accept formatted international
    inputs too, but never guess a home country for a national or invalid number.
    Non-geographic services (e.g. +800) have no country.
    """
    if not phone or len(phone) > 64 or not re.fullmatch(r"\+?[0-9 ()\-\.]+", phone):
        return None
    digits = re.sub(r"[^0-9]", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    if not re.fullmatch(r"[1-9][0-9]{6,14}", digits):
        return None
    try:
        number = phonenumbers.parse("+" + digits, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    region = phonenumbers.region_code_for_number(number)
    return region if region in phonenumbers.SUPPORTED_REGIONS else None


@lru_cache(maxsize=256)
def _country_locale(country: str) -> str | None:
    languages = get_official_languages(country, de_facto=True)
    if not languages:
        return None
    language = languages[0]
    # Preserve regional vocabulary (especially pt-PT versus pt-BR).
    return normalize_locale(f"{language}-{country}") or normalize_locale(language)


def resolve_language(phone: str | None, preferred_locale: str | None = None) -> ConversationLanguage:
    """One policy shared by prompts, template selection and operator views."""
    from app.core.config import get_settings

    settings = get_settings()
    country = phone_country(phone)
    preference = normalize_locale(preferred_locale)
    country_locale = None
    if country:
        country_locale = settings.country_language_overrides.get(country) or _country_locale(country)
    code = preference or country_locale or settings.default_conversation_language
    locale = Locale.parse(code.replace("-", "_"))
    english = Locale.parse("en")
    return ConversationLanguage(
        country_code=country,
        country_name=english.territories.get(country) if country else None,
        language_code=code,
        language_name=locale.get_display_name("en"),
        source="customer_preference" if preference else "phone_country" if country_locale else "fallback",
    )
