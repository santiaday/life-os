# ingest_whoop_journal image. Built from repo root.

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
      'boto3>=1.35'

COPY lifeos_core ./lifeos_core
COPY ingest_whoop_journal ./ingest_whoop_journal

ENTRYPOINT ["python", "-m", "ingest_whoop_journal"]
CMD ["ingest"]
