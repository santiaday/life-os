# mcp_server image. Long-running, exposed via Caddy. Built from repo root.

# Stage 1: build the cronometer-export Go binary. The MCP server's
# refresh_data('cronometer') tool and the auto-sync inside log_food /
# delete_food_entry both shell out to this binary, so it has to live inside
# the mcp container (not just the ingest_cronometer / scheduler images).
FROM golang:1.22-alpine AS cron_builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src
RUN git clone --depth=1 https://github.com/jrmycanady/cronometer-export.git . \
 && CGO_ENABLED=0 go build -o /out/cronometer-export .

# Stage 2: Python runtime.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CRONOMETER_EXPORT_BINARY=/usr/local/bin/cronometer-export

WORKDIR /app

COPY --from=cron_builder /out/cronometer-export /usr/local/bin/cronometer-export
RUN chmod +x /usr/local/bin/cronometer-export

COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && pip install \
      'psycopg[binary,pool]>=3.2' \
      'fastapi>=0.115' \
      'uvicorn[standard]>=0.32' \
      'httpx>=0.27' \
      'tenacity>=9.0' \
      'pydantic>=2.9' \
      'pydantic-settings>=2.5' \
      'structlog>=24.4' \
      'sentry-sdk>=2.14' \
      'mcp>=1.0' \
      'scipy>=1.14' \
      'numpy>=2.1' \
      'apscheduler>=3.10' \
      'anthropic>=0.39' \
      'google-api-python-client>=2.140' \
      'google-auth-oauthlib>=1.2' \
      'google-auth>=2.34' \
      'boto3>=1.35' \
      'opentelemetry-sdk>=1.27' \
      'opentelemetry-exporter-otlp-proto-http>=1.27'

# MCP server now exposes write/refresh tools that call into every ingester
# and the mart refresher, so all packages must be present in the image.
COPY lifeos_core ./lifeos_core
COPY mcp_server ./mcp_server
COPY lifelog_api ./lifelog_api
COPY ingest_whoop ./ingest_whoop
COPY ingest_calendar ./ingest_calendar
COPY ingest_cronometer ./ingest_cronometer
COPY ingest_copilot ./ingest_copilot
COPY ingest_whoop_journal ./ingest_whoop_journal
COPY ingest_whoop_labs ./ingest_whoop_labs
COPY ingest_hevy ./ingest_hevy
COPY ingest_pushpress ./ingest_pushpress
COPY coach ./coach
COPY mart_refresh ./mart_refresh

EXPOSE 8080
ENTRYPOINT ["python", "-m", "mcp_server"]
