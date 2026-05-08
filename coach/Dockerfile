# coach image. Built from repo root: `docker build -f coach/Dockerfile .`
# Runs as a one-shot subprocess invoked by the scheduler service. Bundles
# the mcp_server package because the Hevy routine writer reuses
# mcp_server.hevy_write_tools.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && pip install \
      'psycopg[binary,pool]>=3.2' \
      'httpx>=0.27' \
      'tenacity>=9.0' \
      'pydantic>=2.9' \
      'pydantic-settings>=2.5' \
      'structlog>=24.4' \
      'sentry-sdk>=2.14' \
      'anthropic>=0.39'

COPY lifeos_core ./lifeos_core
COPY ingest_hevy ./ingest_hevy
COPY mcp_server ./mcp_server
COPY coach ./coach

ENTRYPOINT ["python", "-m", "coach"]
CMD ["run"]
