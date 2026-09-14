# Country and conversation language

## Codebase analysis

The backend is a FastAPI service with SQLAlchemy persistence, a Redis worker
queue, Meta WhatsApp integration and an OpenAI structured-output sales agent.
The React dashboard consumes authenticated reporting endpoints.

The signed Meta webhook stores an event and queues processing. The worker calls
`app/services/conversation_flow.py`, which commits the inbound message before
generating a reply. `app/ai/agent.py` builds context, calls the model and validates
its decision; the conversation flow applies permitted business actions and
`app/services/messaging.py` sends and records the outbound message. Follow-ups
use the same agent while inside the customer service window, and approved Meta
templates outside it. Per-conversation locks and existing idempotency checks
continue to govern processing.

Previously, the prompt simply asked the model to match the customer's language.
`Customer.locale` existed but incoming WhatsApp messages did not populate it.
There was no backend phone-country language policy, and the dashboard's country
display used a small prefix list.

## Resolution policy

`app/localization.py` is the single resolver for prompts, templates and the
customer detail API. It uses offline phone metadata from
[python-phonenumbers](https://github.com/daviddrysdale/python-phonenumbers) and
country/language names and official-language data from
[Babel/Unicode CLDR](https://babel.pocoo.org/en/latest/api/languages.html).
Dependencies are pinned in `requirements.txt`; update the pins periodically
and rerun the country regression tests as numbering plans evolve.

The resolver validates a full international number, resolves its country using
the entire number (including shared calling codes such as +1), then chooses:

1. A valid explicit preference in `Customer.locale`.
2. A configured override for the phone's country.
3. CLDR's most widely used official or de-facto official language, with a
   country variant where available.
4. `DEFAULT_CONVERSATION_LANGUAGE` for unknown numbers or missing language data.

Examples: ES → Spain / es-ES / Spanish; PT → Portugal / pt-PT / Portuguese;
BR → Brazil / pt-BR / Portuguese; CA → Canada / en-CA / English;
IN → India / hi-IN / Hindi. Multilingual countries have a business default,
not a claim that every resident speaks that language. Phone country does not
prove current location, nationality or personal preference.

Country detection accepts WhatsApp digits, '+' and formatted international
numbers, including '00' notation. Invalid, unresolvable and non-geographic
numbers safely fall back without rejecting the incoming message. National
numbers without a country code cannot be reliably resolved; no home region is
assumed. Customer identity normalization is unchanged.

The country is derived on every prompt build, so existing customers and later
follow-ups work without a migration or bulk backfill. Only country-level
language metadata is cached; phone numbers are not cached. No additional
network request or LLM classification call is introduced per message.

## Explicit preferences

The prompt instructs the model to use the backend language consistently, even
when examples or short greetings are in English. When a customer explicitly
asks to switch languages, the model replies in that language immediately and
reports `preferred_language` in its structured decision. Supported locale tags
are normalized and persisted in the existing `Customer.locale` field for usable
inbound decisions. Invalid tags are discarded; unprompted follow-ups cannot
change preferences. Ordinary message text does not implicitly change them.

The preference survives a truncated history, new conversations and worker
restarts. A pre-existing valid locale is also respected. As with intent and
platform classification, recognizing an explicit request depends on the model;
the deterministic tests verify the contract and persistence, not model fluency.

The language policy is the final system block, after the sales objective. It
explicitly lets the latest language request override the saved preference; the
stored language is a default, never a restriction. Sales objectives also yield
to a new explicit download, opt-out or human request. These priorities were
clarified after testing real model responses across successive language changes.

## Configuration and deployment

Install the updated `requirements.txt` and restart the API and workers. No
database migration is required. Rebuild the dashboard with
`npm run build --prefix dashboard` to show the new profile fields.

```dotenv
DEFAULT_CONVERSATION_LANGUAGE=en
COUNTRY_LANGUAGE_OVERRIDES={"IN":"en-IN","ES":"es-ES","PT":"pt-PT","BR":"pt-BR"}
WHATSAPP_TEMPLATE_LANGUAGES={"boomshare_followup":{"pt":"pt_BR","es":"es"}}
WHATSAPP_TEMPLATE_BODIES={"boomshare_followup":{"en":"Hi {{1}}, ...","pt_BR":"Oi {{1}}, ...","es":"Hola {{1}}, ..."}}
```

Country keys are ISO region codes and values are supported language/locale
tags. Invalid configuration fails at startup. Defaults are active without any
configuration; the overrides above are the deployed market choices, and `IN`
is one: CLDR's default for +91 is `hi-IN`, and this business answers it in
English.

Template mappings must list translations already approved for that template
name in Meta. Selection checks an exact locale, then the base language, so one
approved `pt_BR` serves Brazil and Portugal and one `es` every Spanish market.
If no variant is configured, the existing lead/follow-up template language
remains the fallback for backward compatibility (English by default). Thus
localized out-of-window delivery requires approved translations and
configuration; the LLM cannot translate an approved template at send time.

`WHATSAPP_TEMPLATE_BODIES` mirrors the approved wording, which Meta owns and
reviews. It is what a sent template is stored as, in the language that actually
went out - the dashboard thread and the model's own history read from there, so
the entries must match WhatsApp Manager word for word and be updated whenever
it changes. A language with no entry is still sent; it is recorded as
`[template:name, language:code]` rather than as an invented body.

The check-in ladder decides which of those paths a follow-up takes.
`FOLLOW_UP_LADDER_HOURS` gaps are measured from the check-in before them, so it
is the running total against `SERVICE_WINDOW_HOURS` that matters: `[4, 22, 72]`
puts the first nudge at 4h, written by the model inside the window, and the
next two at 26h and 98h, sent as the approved template. A total that lands on
the window exactly is decided by scheduler jitter rather than configuration,
and a first gap at or beyond it fails at startup.

The customer-detail response adds `language` with country code/name, effective
language code/name and source. `locale` continues to mean explicit preference;
it is not overwritten by inferred defaults.

## Validation

`tests/test_localization.py` covers country and regional-language detection,
shared calling codes, malformed/non-geographic numbers, configuration,
signed webhook → queue → worker → prompt → persisted outbound → Meta boundary,
customer-detail reporting, durable preferences beyond the history limit,
follow-ups, approved template selection/fallbacks, and an out-of-window
Brazilian follow-up delivered as the approved `pt_BR` template and stored as
its approved body. `tests/test_reminders.py` covers the ladder either side of
the service window, including the rungs that are sent as templates.

`scripts/e2e_smoke.py` additionally runs real HTTP, uvicorn, migrations, workers
and the real Meta/OpenAI clients. Its Spain/Portugal/Brazil scenarios inspect
the serialized model prompt and Unicode WhatsApp payload, then verify a saved
language change. Only provider HTTP responses are scripted; SQLite and
fakeredis replace production stores. It sends no real customer messages and
does not establish live-provider fluency or template approval.

```bash
python -m pytest
python scripts/e2e_smoke.py
ruff check .
npm run build --prefix dashboard
```

For a real-model run with synthetic inbound scenarios and captured delivery:

```bash
python scripts/live_language_check.py
```

This creates a separate SQLite database under `artifacts/language-qa-20260914`
and calls the configured OpenAI model with real credentials. No production
customer state is changed. To send the four language-switch test replies to an
explicitly authorized test recipient with an open WhatsApp service window:

```bash
python scripts/live_language_check.py --send-to COUNTRYCODE_AND_NUMBER --output artifacts/language-live
```

The transport allowlists that exact number and captures all other deliveries.
Synthetic incoming messages exercise the signed HTTP webhook, queue, worker,
prompt, real model, guardrails and real Meta client. A Meta HTTP 200 establishes
acceptance, not handset delivery; confirm delivery separately with receipts or
the recipient. Review the recorded text for language quality and regional usage;
the script asserts delivery, requested preference persistence and download links.
