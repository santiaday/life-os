# mcp_server image. Long-running, exposed via Caddy. Built from repo root.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

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
      'google-api-python-client>=2.140' \
      'google-auth-oauthlib>=1.2' \
      'google-auth>=2.34'

# MCP server now exposes write/refresh tools that call into every ingester
# and the mart refresher, so all packages must be present in the image.
COPY lifeos_core ./lifeos_core
COPY mcp_server ./mcp_server
COPY ingest_whoop ./ingest_whoop
COPY ingest_calendar ./ingest_calendar
COPY ingest_cronometer ./ingest_cronometer
COPY ingest_copilot ./ingest_copilot
COPY mart_refresh ./mart_refresh

EXPOSE 8080
ENTRYPOINT ["python", "-m", "mcp_server"]
