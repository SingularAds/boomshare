# Deploying to Cloud Run

Target: GCP project `smbaicallz`, region `southamerica-east1`.

- **Database:** Cloud SQL for PostgreSQL, instance connection name
  `smbaicallz:southamerica-east1:boom-share`, database `my-boomshare`.
- **Redis:** the existing hosted instance
  (`tooth-vespertine-cabbage-40983.db.redis.io:19501`) — public endpoint, no VPC
  connector needed.
- **Two services from one image:** `boomshare-api` (public) and
  `boomshare-worker` (internal). Same container, different command.

The database is reached over the Cloud SQL **Unix socket** that Cloud Run mounts
at `/cloudsql/<INSTANCE_CONNECTION_NAME>` when you pass
`--add-cloudsql-instances`. No Cloud SQL Auth Proxy sidecar is required on Cloud
Run.

---

## `DATABASE_URL` format

```
postgresql+asyncpg://postgres:<PASSWORD>@/my-boomshare?host=/cloudsql/smbaicallz:southamerica-east1:boom-share
```

- User is assumed to be **`postgres`** (the Cloud SQL default, and what your
  local `.env` uses). If you created a dedicated user, change it.
- `<PASSWORD>` must be **URL-encoded**: `@` → `%40`, `:` → `%3A`, `/` → `%2F`,
  etc. `BoomShare@123` becomes `BoomShare%40123`.
- The `?host=/cloudsql/...` form makes asyncpg connect via the Unix socket.
  Verified against this codebase — the app and `alembic` both accept it.

---

## One-time setup

```bash
PROJECT=smbaicallz
REGION=southamerica-east1

gcloud config set project $PROJECT

# 1. APIs
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

# 2. Artifact Registry repo for the image
gcloud artifacts repositories create boomshare \
  --repository-format=docker --location=$REGION

# 3. Secrets. Create each from a local value, never commit the values.
#    The secret names match the env var names the app reads. Substitute the
#    real values (URL-encode the DB password: @ -> %40).
printf '%s' 'postgresql+asyncpg://postgres:<URL_ENCODED_DB_PASSWORD>@/my-boomshare?host=/cloudsql/smbaicallz:southamerica-east1:boom-share' \
  | gcloud secrets create DATABASE_URL --data-file=-

printf '%s' 'redis://default:<REDIS_PASSWORD>@tooth-vespertine-cabbage-40983.db.redis.io:19501/0' \
  | gcloud secrets create REDIS_URL --data-file=-

printf '%s' '<META_APP_SECRET>'    | gcloud secrets create META_APP_SECRET --data-file=-
printf '%s' '<META_VERIFY_TOKEN>'  | gcloud secrets create META_VERIFY_TOKEN --data-file=-
printf '%s' '<META_ACCESS_TOKEN>'  | gcloud secrets create META_ACCESS_TOKEN --data-file=-
printf '%s' '<OPENAI_API_KEY>'     | gcloud secrets create OPENAI_API_KEY --data-file=-
printf '%s' '<INTERNAL_API_TOKEN>' | gcloud secrets create INTERNAL_API_TOKEN --data-file=-
printf '%s' '<ADMIN_API_TOKEN>'    | gcloud secrets create ADMIN_API_TOKEN --data-file=-

# 4. Let the Cloud Run runtime service account read secrets and reach Cloud SQL.
SA="$(gcloud iam service-accounts list --filter='displayName:Compute Engine default' --format='value(email)')"
for ROLE in roles/secretmanager.secretAccessor roles/cloudsql.client; do
  gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:${SA}" --role="$ROLE"
done
```

> Use a dedicated service account instead of the Compute Engine default if you
> want least-privilege. Pass it to every `gcloud run deploy` with
> `--service-account`.

To rotate a secret later: `gcloud secrets versions add NAME --data-file=-` then
redeploy (the deploy pins `:latest`).

---

## Deploy

```bash
./deploy/deploy.sh                 # tag = short git SHA
./deploy/deploy.sh v1.4.0          # explicit tag
```

The script: builds the image with Cloud Build, runs `alembic upgrade head` as a
Cloud Run **job** against the instance, then deploys `boomshare-api` and
`boomshare-worker`. Non-secret config is `deploy/env.prod.yaml`; secrets are
wired from Secret Manager.

Migrations run as their own step **before** the new revision serves traffic —
never coupled to app startup.

---

## After the first deploy

1. **Meta webhook** → point at `https://<api-url>/webhooks/meta`, verify token =
   the `META_VERIFY_TOKEN` secret, subscribe to `messages` **and** `leadgen`.
2. **Templates** — `boomshare_lead_intro` and `boomshare_followup` must be
   **APPROVED** in WhatsApp Manager (see `docs/meta-setup.md` §9).
3. **Product facts** — replace `app/ai/knowledge/product.md` with real content
   and rebuild, or the AI sells placeholder copy.
4. **Download page** — `DOWNLOAD_BASE_URL` must forward `?ref=` to
   `POST /internal/events/click`; wire the desktop backend to
   `/internal/events/{download,activation}`.
5. **Check it:**
   ```bash
   curl https://<api-url>/health           # {"status":"ok","environment":"production"}
   curl https://<api-url>/health/ready      # database ok, redis ok
   ```
   The worker is internal — check it from Cloud Run logs, or:
   ```bash
   gcloud run services proxy boomshare-worker --region southamerica-east1 &
   curl localhost:8080/health/ready
   ```

---

## Notes specific to Cloud Run

| Concern | How it's handled |
|---|---|
| `$PORT` | The image's `CMD` binds `$PORT` (Cloud Run injects 8080). The worker serves the same probe via `app/worker/health.py`. |
| Worker as a service | Cloud Run requires a port listener; the worker has one now. `--no-cpu-throttling` keeps the drain loop alive between requests; `--min-instances 1 --max-instances 1` because conversation locking is still Redis-only (see the deploy-readiness review, risk R2). |
| Graceful shutdown | `CMD` uses `exec`, so uvicorn / the worker is PID 1 and gets `SIGTERM` directly. Both drain and exit 0. Verified. |
| DB connections | Cloud SQL small tiers cap `max_connections` low. `DB_POOL_SIZE` (5) × api `--max-instances` (4) + worker ≈ 25. Check `SHOW max_connections;` and lower either if you see "too many clients". |
| Redis over public internet | The worker no longer busy-loops if Redis blips (`consume` backs off). A blip still delays jobs by the sweep interval, not data. Consider Memorystore + a Serverless VPC connector later to keep traffic private. |
| Cost | `--no-cpu-throttling` + `--min-instances 1` on two services means you pay for ~2 always-on vCPUs. Expected for an always-listening worker. |

---

## Rollback

```bash
gcloud run services update-traffic boomshare-api    --region southamerica-east1 --to-revisions PREVIOUS=100
gcloud run services update-traffic boomshare-worker --region southamerica-east1 --to-revisions PREVIOUS=100
```

If a migration must be undone, run `alembic downgrade -1` via the same job
pattern (`--args "downgrade,-1"`) **before** rolling app code back.
