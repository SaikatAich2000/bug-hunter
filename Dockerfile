# --- Bug Hunter — production-style image, intentionally small ---
#
# BASE_IMAGE is overridable so deployments behind a corporate proxy or
# air-gapped network can point at an internal registry mirror without
# editing this file:
#
#   BASE_IMAGE=mirror.internal/python:3.12-slim ./deploy.sh
#
# Default is the public Docker Hub tag.
#
# Supply-chain hardening (recommended for reproducible/audited builds): pin the
# base image by DIGEST and refresh it via Dependabot/renovate, e.g.
#   BASE_IMAGE=python:3.12-slim@sha256:<digest> ./deploy.sh
# and install with a hash-locked requirements file (pip-compile --generate-hashes
# then `pip install --require-hashes`). Left as a floating tag by default so the
# out-of-the-box build doesn't break when the upstream digest rotates.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

# Don't write .pyc files, flush logs immediately, no pip version-check chatter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg[binary] ships its own libpq, so we don't need build-essential
# or libpq-dev. Keep the image lean — just curl for the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer caches across code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy application code
COPY app ./app

# Run as a non-root user
RUN useradd --create-home --shell /bin/bash appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Container-level healthcheck hitting the app's /api/health endpoint, which now
# returns HTTP 503 (not 200) when the database is unreachable — so `curl -fsS`
# correctly reports the container unhealthy while the DB is down, instead of
# masking a degraded app. start-period gives the DB + first boot time to come up
# before failures count against the retry budget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
