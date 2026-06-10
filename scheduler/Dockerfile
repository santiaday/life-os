# scheduler image. Bundles every ingester package so jobs can `python -m
# ingest_<source>` directly (no Docker-in-Docker). Built from repo root.

# Stage 1: Cronometer Go binary, copied in below.
FROM golang:1.22-alpine AS cron_builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src
RUN git clone --depth=1 https://github.com/jrmycanady/cronometer-export.git . \
 && CGO_ENABLED=0 go build -o /out/cronometer-export .

# Stage 2: Python runtime
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York \
    CRONOMETER_EXPORT_BINARY=/usr/local/bin/cronometer-export

WORKDIR /app

COPY --from=cron_builder /out/cronometer-export /usr/local/bin/cronometer-export
RUN chmod +x /usr/local/bin/cronometer-export

COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && pip install \
      'psycopg[binary,pool]>=3.2' \
      'apscheduler>=3.10' \
      'httpx>=0.27' \
      'tenacity>=9.0' \
      'pydantic>=2.9' \
      'pydantic-settings>=2.5' \
      'structlog>=24.4' \
      'sentry-sdk>=2.14' \
      'google-api-python-client>=2.140' \
      'google-auth-oauthlib>=1.2' \
      'google-auth>=2.34' \
      'boto3>=1.35' \
      'anthropic>=0.39'

# Copy every service package — scheduler invokes them as subprocesses.
COPY lifeos_core ./lifeos_core
COPY lifelog_api ./lifelog_api
COPY ingest_whoop ./ingest_whoop
COPY ingest_calendar ./ingest_calendar
COPY ingest_cronometer ./ingest_cronometer
COPY ingest_copilot ./ingest_copilot
COPY ingest_whoop_journal ./ingest_whoop_journal
COPY ingest_whoop_private ./ingest_whoop_private
COPY ingest_whoop_labs ./ingest_whoop_labs
# Hevy / PushPress / coach were deprecated — their packages are no longer
# scheduled, so they're not bundled into the scheduler image. Historical data
# and the read-only MCP tools live on; only the cron-driven ingest/coach
# subprocesses were removed.
COPY mart_refresh ./mart_refresh
COPY calendar_sync ./calendar_sync
COPY scheduler ./scheduler

ENTRYPOINT ["python", "-m", "scheduler"]
