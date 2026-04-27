# Multi-stage: build the Go `cronometer-export` binary, then assemble a Python
# runtime image that calls it via subprocess. Built from repo root:
#   docker build -t ghcr.io/santi/life-os-ingest-cronometer:latest \
#                -f ingest_cronometer/Dockerfile .

# ---- stage 1: Go binary -----------------------------------------------------
FROM golang:1.22-alpine AS builder
RUN apk add --no-cache git ca-certificates
WORKDIR /src
RUN git clone --depth=1 https://github.com/jrmycanady/cronometer-export.git . \
 && CGO_ENABLED=0 go build -o /out/cronometer-export .

# ---- stage 2: Python runtime ------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CRONOMETER_EXPORT_BINARY=/usr/local/bin/cronometer-export

WORKDIR /app

COPY --from=builder /out/cronometer-export /usr/local/bin/cronometer-export
RUN chmod +x /usr/local/bin/cronometer-export

COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && pip install \
      'psycopg[binary,pool]>=3.2' \
      'pydantic>=2.9' \
      'pydantic-settings>=2.5' \
      'structlog>=24.4' \
      'sentry-sdk>=2.14'

COPY lifeos_core ./lifeos_core
COPY ingest_cronometer ./ingest_cronometer

ENTRYPOINT ["python", "-m", "ingest_cronometer"]
CMD ["ingest"]
