# Multi-stage so the runtime image carries no build toolchain.
FROM python:3.12-slim AS build

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

# Pillow needs headers to build wheels for some platforms; they stay in this
# stage and never reach the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libjpeg-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Dependencies resolve from pyproject; copying it alone keeps this layer cached
# across source-only changes.
RUN pip install --no-cache-dir --prefix=/install ".[dev]" || \
    pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim AS runtime

# Runtime needs the shared libs Pillow links against, not the -dev headers.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g curl \
 && rm -rf /var/lib/apt/lists/*

# Never run as root.
RUN useradd --create-home --uid 10001 rentsafe
WORKDIR /app

COPY --from=build /install /usr/local
COPY --chown=rentsafe:rentsafe . .

# Uploaded media lands here only in development. Production sets MEDIA_BUCKET
# and the app refuses to boot without it, because a container filesystem is
# ephemeral and a redeploy would take every tenant photo with it.
RUN mkdir -p /app/data/media /app/data/cache && chown -R rentsafe:rentsafe /app/data
VOLUME ["/app/data"]

USER rentsafe

ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000

# Readiness is checked by the orchestrator against /health/ready, which
# verifies the database and cache rather than just that Python is alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/health/ready" || exit 1

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
