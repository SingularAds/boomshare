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
    # How long an idle HTTPS connection to Meta or OpenAI is kept open. httpx
    # defaults this to 5 seconds, which is shorter than the gap between two
    # messages in a real conversation (measured: 24-44s), so every customer
    # message paid a fresh TLS handshake to both providers. Comfortably longer
    # than that gap, and comfortably shorter than the idle timeout an upstream
    # load balancer is likely to enforce.
    http_keepalive_seconds: float = 120.0
    # Every WhatsApp number this deployment answers on, most important first.
    # They all arrive at the same webhook - Meta signs with the app secret, not
    # per number - so this list exists to answer two questions the payload
    # cannot: is this number ours, and which one do we send from when there is
    # no inbound message to answer (a lead ad). The first entry is that default.
    whatsapp_phone_number_ids: tuple[str, ...] = ()
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
    # How long a turn will queue behind the worker that already holds this
    # conversation, instead of giving up. Giving up is expensive: the job is
    # parked as `failed`, it burns one of `worker_max_attempts`, and nothing
    # retries it until the sweep - so a customer who sent two messages in quick
    # succession waited `webhook_sweep_after_seconds` for the second answer.
    # Comfortably shorter than `ai_reply_lock_seconds`, so a lock whose holder
    # died is waited out by its TTL rather than by this.
    ai_reply_lock_wait_seconds: float = 20.0
    inbound_rate_limit_per_minute: int = 20
    # Automatic check-ins when a live conversation ends a turn with nothing
    # queued: one entry per unanswered nudge, and the length of the list is the
    # cap. Someone who ignored two messages will not answer a third an hour
    # later, and continuing to send them is what costs a WhatsApp number its
    # quality rating. The first gap stays under `service_window_hours` so that
    # nudge can be a normal AI message rather than a template.
    follow_up_ladder_hours: tuple[float, ...] = (4.0, 20.0, 72.0)

    # ---- worker ----------------------------------------------------------
    run_embedded_worker: bool = True
    worker_queue_name: str = "boomshare:jobs"
    # How many jobs one worker runs at a time. The pipeline is almost entirely
    # waiting - on OpenAI, on the Graph API, on the database - so a serial
    # consumer left one customer queued behind another's model call for no
    # reason. Replies to the *same* conversation still serialise, on the Redis
    # lock; this only lets unrelated conversations overlap. Keep it at or below
    # `db_pool_size` so a full batch cannot outnumber the connection pool.
    worker_concurrency: int = 4
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

    @field_validator("whatsapp_phone_number_ids", mode="after")
    @classmethod
    def _clean_phone_number_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject the two ways this list goes wrong when a number is added.

        A blank entry would send to `/v26.0//messages`, and a duplicate would
        make "which number is the default" depend on list order in a way nobody
        would think to check. Both are typos at deploy time, so they fail here
        rather than on the first customer message.
        """
        cleaned = [str(item).strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("WHATSAPP_PHONE_NUMBER_IDS contains a blank entry")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(f"WHATSAPP_PHONE_NUMBER_IDS contains duplicates: {cleaned}")
        return tuple(cleaned)

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
            "WHATSAPP_PHONE_NUMBER_IDS": self.default_phone_number_id,
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
    def default_phone_number_id(self) -> str:
        """The number we send from when nothing tells us which one to use.

        Only lead ads reach this: a lead-ad submission has no phone number in
        it, because the customer has not messaged us yet. Every reply to an
        actual message is routed by the conversation it belongs to.
        """
        return self.whatsapp_phone_number_ids[0] if self.whatsapp_phone_number_ids else ""

    def knows_phone_number(self, phone_number_id: str | None) -> bool:
        """Is this one of our numbers?

        A webhook for a number we do not know means Meta and this deployment
        disagree about what we own - usually a number added in Business Manager
        and not in the environment. We store the message either way; we just do
        not answer from an identity we cannot vouch for.
        """
        return bool(phone_number_id) and phone_number_id in self.whatsapp_phone_number_ids

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
