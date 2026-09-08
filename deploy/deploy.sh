#!/usr/bin/env bash
# Build, migrate, and deploy the API + worker to Cloud Run.
#
#   ./deploy/deploy.sh                # tag = short git SHA
#   ./deploy/deploy.sh v1.2.3         # explicit tag
#
# One-time setup (Secret Manager, Artifact Registry, IAM) is in deploy/README.md.
set -euo pipefail

PROJECT="smbaicallz"
REGION="southamerica-east1"
SQL_INSTANCE="smbaicallz:southamerica-east1:boom-share"
AR_REPO="boomshare"                       # Artifact Registry repo (create once)
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/boomshare"
TAG="${1:-$(git rev-parse --short HEAD)}"
REF="${IMAGE}:${TAG}"

# Every secret the app reads, as NAME=SECRET:VERSION. The Secret Manager secret
# names match the env var names (see README.md for how they were created).
SECRETS="DATABASE_URL=DATABASE_URL:latest,\
REDIS_URL=REDIS_URL:latest,\
META_APP_SECRET=META_APP_SECRET:latest,\
META_VERIFY_TOKEN=META_VERIFY_TOKEN:latest,\
META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest,\
OPENAI_API_KEY=OPENAI_API_KEY:latest,\
INTERNAL_API_TOKEN=INTERNAL_API_TOKEN:latest,\
ADMIN_API_TOKEN=ADMIN_API_TOKEN:latest"

echo ">> Building ${REF}"
gcloud builds submit --project "$PROJECT" --tag "$REF" .

echo ">> Running migrations (Cloud Run job)"
# alembic only needs DATABASE_URL; ENVIRONMENT is left at its default so the
# production secret check does not force every secret onto the migrate job.
gcloud run jobs deploy boomshare-migrate \
  --project "$PROJECT" --region "$REGION" \
  --image "$REF" \
  --add-cloudsql-instances "$SQL_INSTANCE" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --command alembic --args "upgrade,head" \
  --max-retries 1 --task-timeout 600s
gcloud run jobs execute boomshare-migrate --project "$PROJECT" --region "$REGION" --wait

echo ">> Deploying API service"
gcloud run deploy boomshare-api \
  --project "$PROJECT" --region "$REGION" \
  --image "$REF" \
  --add-cloudsql-instances "$SQL_INSTANCE" \
  --env-vars-file deploy/env.prod.yaml \
  --set-secrets "$SECRETS" \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 4 \
  --cpu 1 --memory 512Mi --concurrency 40 \
  --timeout 60s

echo ">> Deploying worker service"
# The worker drains the queue; it answers the platform probe on $PORT via
# app/worker/health.py. --no-cpu-throttling keeps it running between requests;
# pinned to one instance (see the deploy-readiness note on conversation locking).
gcloud run deploy boomshare-worker \
  --project "$PROJECT" --region "$REGION" \
  --image "$REF" \
  --add-cloudsql-instances "$SQL_INSTANCE" \
  --env-vars-file deploy/env.prod.yaml \
  --set-secrets "$SECRETS" \
  --command python --args "-m,app.worker.runner" \
  --no-cpu-throttling \
  --min-instances 1 --max-instances 1 --concurrency 1 \
  --cpu 1 --memory 512Mi \
  --no-allow-unauthenticated

API_URL="$(gcloud run services describe boomshare-api --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
echo ">> Done. API: ${API_URL}"
echo ">> Point the Meta webhook at ${API_URL}/webhooks/meta and subscribe to messages + leadgen."
curl -fsS "${API_URL}/health" && echo
