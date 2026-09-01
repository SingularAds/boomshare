# Single image, two entrypoints: the API and the worker run the same code with
# different commands (see docker-compose.yml). Keeps deploys simple and makes
# it impossible for the two to drift apart.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build tools are needed for some wheels but not at runtime; installing
# dependencies before copying source keeps this layer cached across code edits.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 boomshare \
    && chown -R boomshare:boomshare /app
USER boomshare

# Cloud Run (and App Engine) inject PORT and expect the container to listen on
# it; everywhere else this default holds. Uvicorn binds $PORT at start.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# Run migrations automatically before starting uvicorn.
# `exec` so uvicorn replaces the shell as PID 1 and receives SIGTERM directly.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=*"]
