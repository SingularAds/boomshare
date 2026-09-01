"""Application configuration.

Everything is environment driven. Secrets are `SecretStr` so they never end up
in a repr / log line by accident.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The local-development defaults. A staging / production boot that still carries
# either of these has not been given real infrastructure - fail fast rather than
# quietly connecting to a database that is not there.
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://boomshare:boomshare@localhost:5432/boomshare"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- app -------------------------------------------------------------
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    # ---- datastores ------------------------------------------------------
    database_url: str = _DEFAULT_DATABASE_URL
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    redis_url: str = _DEFAULT_REDIS_URL
    # The MVP runs on a 30MB Redis tier: everything we put there gets a TTL.
    redis_default_ttl_seconds: int = 3600

    # ---- Meta / WhatsApp -------------------------------------------------
    meta_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    meta_access_token: SecretStr = SecretStr("")
    meta_graph_version: str = "v26.0"
    meta_graph_base_url: str = "https://graph.facebook.com"
    whatsapp_phone_number_id: str = ""
    # Approved template used to open a conversation with a lead-ad lead.
    whatsapp_lead_template_name: str = "boomshare_lead_intro"
    whatsapp_lead_template_language: str = "en"
    whatsapp_followup_template_name: str = "boomshare_followup"
    whatsapp_followup_template_language: str = "en"

    # ---- OpenAI ----------------------------------------------------------
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 2
    openai_temperature: float = 0.6
    openai_max_output_tokens: int = 700

    # ---- internal auth ---------------------------------------------------
    # Used by the Boomshare desktop app / admin tooling to call back into us.
    internal_api_token: SecretStr = SecretStr("")

    # ---- product ---------------------------------------------------------
    download_base_url: str = "https://boomshare.ai/download"
    # Max characters we allow the AI to send in one WhatsApp message.
    max_reply_characters: int = 900

    # ---- conversation policy --------------------------------------------
    # Meta's customer service window: free-form replies are only allowed
    # within 24h of the customer's last inbound message.
    service_window_hours: int = 24
    history_message_limit: int = 20
    ai_reply_lock_seconds: int = 30
    inbound_rate_limit_per_minute: int = 20
    # Automatic check-in when a live conversation ends a turn with nothing
    # queued. Kept under `service_window_hours` on purpose: inside the window
    # the follow-up can be a normal AI message instead of a template.
    default_follow_up_hours: float = 20.0

    # ---- worker ----------------------------------------------------------
    worker_queue_name: str = "boomshare:jobs"
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 20
    worker_max_attempts: int = 5
    # How long a webhook event may sit unfinished before the sweeper retries it.
    # Must comfortably exceed one full reply turn's worst case, or the sweeper
    # can re-enqueue a job that is still running (a slow OpenAI call retries up
    # to `openai_max_retries` times at `openai_timeout_seconds` each, plus the
    # Meta client's own retries). 180s covers the default 30s x 3 with margin.
    webhook_sweep_after_seconds: int = 180
    scheduler_interval_seconds: float = 15.0

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _require_secrets_when_deployed(self) -> Settings:
        """Fail fast on a misconfigured staging / production boot.

        Locally and under test these may be blank. Once `ENVIRONMENT` says this
        is a real deployment, a missing secret means silent failure in traffic -
        every webhook 403s, the AI goes quiet - so refuse to start instead.
        """
        if self.environment not in ("staging", "production"):
            return self

        required = {
            "META_APP_SECRET": self.meta_app_secret.get_secret_value(),
            "META_VERIFY_TOKEN": self.meta_verify_token.get_secret_value(),
            "META_ACCESS_TOKEN": self.meta_access_token.get_secret_value(),
            "WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
            "OPENAI_API_KEY": self.openai_api_key.get_secret_value(),
            "INTERNAL_API_TOKEN": self.internal_api_token.get_secret_value(),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if self.database_url == _DEFAULT_DATABASE_URL:
            missing.append("DATABASE_URL")
        if self.redis_url == _DEFAULT_REDIS_URL:
            missing.append("REDIS_URL")
        if missing:
            raise ValueError(
                f"ENVIRONMENT={self.environment} but these are not set: {', '.join(sorted(missing))}"
            )
        return self

    @property
    def graph_url(self) -> str:
        return f"{self.meta_graph_base_url}/{self.meta_graph_version}"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
