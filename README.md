# Boomshare AI Backend

A WhatsApp AI sales platform. Meta ads bring people in, an OpenAI sales agent
has real conversation with them, and the application drives them toward
installing the Boomshare desktop app — while keeping every businesses decision in
PostgreSQL rather than in the model's head.

- **Docs:** [Architecture](docs/architecture.md) · [Code blueprint](docs/code-blueprint.md) · [Local sandbox](docs/local-sandbox.md) · [Meta setup](docs/meta-setup.md) · [Testing guide](docs/testing-guide.md) · [Operations](docs/operations.md)
- **Stack:** FastAPI · PostgreSQL · Redis · OpenAI · Meta WhatsApp Cloud API
- **Shape:** modular monolith — one deployable, two processes (API + worker)

---

## The two flows

**Flow 1 — Click-to-WhatsApp.** Someone clicks a Meta ad, lands in WhatsApp and
messages us. The webhook arrives with a `referral` block naming the ad. We
identify the customer, record the attribution, and the AI starts selling.

**Flow 2 — Lead ad.** Someone submits a lead form. Meta notifies us, we fetch
the submission from the Graph API, create the customer, and open the
conversation with an **approved template** — because Meta does not allow
free-form messages to someone who has never written to us. When they reply, the
normal AI conversation begins.

Both converge on the same pipeline:

```
Meta webhook → verify signature → store → queue → worker
  → identify customer → persist message → build prompt → OpenAI
  → validate decision → act → send WhatsApp reply
```

---

## Quick start

### With Docker (everything included)

```bash
cp .env.example .env      # then fill in the Meta and OpenAI values
docker compose up --build
```

The API comes up on <http://localhost:8000>, migrations run automatically, and
the worker starts alongside it. Check <http://localhost:8000/health/ready>.

### Without Docker

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements-dev.txt
cp .env.example .env

alembic upgrade head
uvicorn app.main:app --reload                        # terminal 1
python -m app.worker.runner                          # terminal 2
```

You need PostgreSQL and Redis reachable at the URLs in your `.env`.

### With nothing at all — no database, no Redis, no Meta account

**The interactive sandbox.** You chat as the customer and watch the pipeline
run, against a fake Meta that enforces Meta's real rules and returns Meta's
real error codes:

```bash
python scripts/sandbox.py
```

See **[docs/local-sandbox.md](docs/local-sandbox.md)**. Or run the scripted
end-to-end journey instead:

```bash
python scripts/e2e_smoke.py
```

---

## Try it in one minute

With the app running, drive a real (signed) webhook through it:

```bash
# a customer arrives from a click-to-WhatsApp ad
python scripts/send_webhook.py message --text "I want to know more" --referral

# they ask a question
python scripts/send_webhook.py message --text "how much does it cost?"

# a lead-ad submission
python scripts/send_webhook.py leadgen
```

Then look at what happened:

```bash
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" localhost:8000/admin/conversations
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" localhost:8000/admin/reports/funnel
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" localhost:8000/admin/reports/agent
```

Or open <http://localhost:8000/dashboard> and paste the same token into it. The
page is built from `dashboard/` — the Docker image builds it, so a source
checkout needs `cd dashboard && npm install && npm run build` once before the
route serves anything.

Step-by-step verification of every feature is in the
**[testing guide](docs/testing-guide.md)**, and the guide to connecting a real
Meta developer account is **[docs/meta-setup.md](docs/meta-setup.md)**.

---

## Layout

For a file-by-file map and a line-by-line trace of every process, see
[docs/code-blueprint.md](docs/code-blueprint.md).

```
app/
  main.py            FastAPI app factory, middleware, error handlers
  domain.py          sales vocabulary + the state machine  (no I/O)
  models.py          SQLAlchemy models

  api/               HTTP surface — thin, no business logic
    webhooks.py        Meta webhook: verify → store → queue → 200
    internal.py        install/activation events from the Boomshare app
    admin.py           operator console + funnel reporting
    health.py          liveness and readiness

  services/          the business
    conversation_flow.py   the pipeline — read this one first
    conversations.py       conversation lifecycle + sales state transitions
    customers.py           identity and durable lifecycle facts
    leads.py               lead creation for both flows
    attribution.py         campaigns and ads
    messaging.py           outbound WhatsApp + Meta's 24h window rule
    reminders.py           follow-ups
    downloads.py           tracked links, confirmed installs
    webhook_events.py      the durable inbox / idempotency ledger

  ai/                prompt assembly and output validation
    prompts/           sales behaviour        (markdown — edit freely)
    knowledge/         product facts          (markdown — edit freely)
    context.py         builds the prompt from application state
    guardrails.py      validates what the model returns
    agent.py           state → context → model → guardrails

  integrations/      the only modules that talk to the outside world
    meta/              signature, parser, Graph/WhatsApp client
    openai/            the SalesModel protocol and its OpenAI implementation

  worker/            background processing
    queue.py           Redis list queue
    jobs.py            job handlers
    runner.py          consumer loop + scheduler sweeps

  core/              config, db, redis, logging, clock, errors
```

---

## Environment

Every setting is environment-driven and documented inline in
[`.env.example`](.env.example). The ones without a default that you must supply:

| Variable | Where to get it |
|---|---|
| `META_APP_SECRET` | Meta App → Settings → Basic → App Secret |
| `META_VERIFY_TOKEN` | Any string you choose; type the same one into Meta |
| `META_ACCESS_TOKEN` | System user token with WhatsApp + leads permissions |
| `WHATSAPP_PHONE_NUMBER_IDS` | WhatsApp Manager → API Setup (the IDs, not the numbers). A JSON list, one per number you answer on; the first is the default sender |
| `OPENAI_API_KEY` | platform.openai.com |
| `INTERNAL_API_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `ADMIN_API_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` — must differ from the internal one |
| `DATABASE_URL` | Your PostgreSQL instance |
| `REDIS_URL` | Your Redis instance |

Secrets are `SecretStr`, never logged, and scrubbed from log output by a
redaction filter. Nothing is hardcoded.

---

## Tests

```bash
pytest                              # 504 tests, ~40s, no network
pytest --cov=app --cov-report=term  # ~92% coverage
pytest tests/test_inbound_flow.py   # one area
```

The suite runs the whole application in-process against SQLite and fakeredis,
with Meta and OpenAI replaced through the same injection points production
uses. `tests/test_meta_contract.py` goes further: it runs the **real**
`MetaClient` against a fake Meta that enforces the Cloud API's actual rules, so
a wrong request shape fails in CI rather than on your first live message. **Sales logic can be changed and verified without spending a token or
sending a WhatsApp message.**

---

## What the AI decides, and what it does not

The model returns a structured decision — reply text, intent, a suggested sales
stage, and requested actions. The application validates all of it before
anything happens:

- URLs written by the model are **stripped**; the backend owns the download
  link and attaches the real, tracked one
- `downloaded` and `activated` are **system-only** stages — no model output can
  reach them, only a confirmed event from the Boomshare desktop backend
- stage changes must be legal transitions in the state machine
- replies claiming an action already happened ("I've sent you the link") are
  refused outright
- follow-up delays are clamped; conflicting actions are dropped

Every decision — including what was rejected and why — is written to
`ai_decisions`, which is what you tune the prompts against.

---

## Editing the sales behaviour

Two markdown files, no code:

- [`app/ai/prompts/sales_agent.md`](app/ai/prompts/sales_agent.md) — tone, method, hard rules
- [`app/ai/knowledge/product.md`](app/ai/knowledge/product.md) — what Boomshare is, features, pricing, limits
- [`app/ai/knowledge/objections.md`](app/ai/knowledge/objections.md) — common questions and pushback

> **Before launch:** the product knowledge currently contains placeholder facts.
> Replace them with real Boomshare data — the AI treats that file as the only
> thing it is allowed to state as fact.

Apply edits without a restart:

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  localhost:8000/admin/prompts/reload
```
