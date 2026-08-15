"""FastAPI application entrypoint.

Run locally:  uvicorn app.main:app --reload
Docs:         http://localhost:8000/docs

On startup (dev): creates tables if missing and seeds Lagos reference/demo data
into the zero-setup SQLite database. Production uses Alembic + sql/001_schema.sql
against Postgres/PostGIS instead.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.v1.router import api_router
from app.config import get_settings
from app.core import observability
from app.db.models import Base
from app.db.seed import seed_if_empty
from app.db.session import SessionLocal, engine
from app.services import otp_store, sms

settings = get_settings()

# Uvicorn configures only its own loggers, so application logs are silent by
# default. In development that hides the console SMS sink — the one place a
# developer can read their own sign-in code. In production this switches to
# JSON so lines are parseable and carry the request ID.
observability.configure_logging(debug=settings.debug)
logger = logging.getLogger("app.access")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.debug:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            await seed_if_empty(session)
    else:
        # Fail at boot, not at first login. build_provider() raises when no SMS
        # key is configured outside development — a deployment that cannot send
        # codes cannot sign anybody in, and it should not accept traffic.
        sms.get_provider()

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            # Review text and phone numbers must never leave the box in a
            # crash report.
            send_default_pii=False,
            traces_sample_rate=0.1,
        )

    yield
    # Release pooled connections so a rolling deploy doesn't leave the database
    # holding sockets for a process that has gone.
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Lagos rental transparency & intelligence platform — API.",
    lifespan=lifespan,
)

# CORS is restricted to the configured origins in every environment (Section XV).
# `allow_origins=["*"]` together with `allow_credentials=True` is rejected by
# browsers anyway, so there is no permissive shortcut worth having here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this a cross-origin browser cannot read the header at all, and the
    # client silently falls back to assuming the page it got is the whole set.
    expose_headers=["X-Total-Count", "X-Request-ID"],
)

app.include_router(api_router)


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline hardening headers.

    The API serves JSON and user-uploaded images. The CSP here is deliberately
    restrictive because nothing it returns should ever be rendered as a
    document — the frontend is a separate origin with its own policy.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
    )
    if not settings.debug:
        # Only meaningful over TLS, and harmful to set while developing on
        # plain http://localhost.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.middleware("http")
async def observe(request, call_next):
    """Assign a request ID, time the request, and record the outcome.

    Registered *last* so it is the outermost middleware: Starlette applies them
    in reverse registration order. Being outermost is the point — a failure
    raised inside any other middleware is still counted, and every response
    carries an ID the user can quote back to us.
    """
    request_id = observability.new_request_id(request.headers.get("x-request-id"))
    token = observability.request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        # Count it, log it with the ID, and let it propagate to the handler that
        # turns it into a 500 — swallowing it here would hide real failures.
        elapsed = time.perf_counter() - started
        observability.metrics.record_exception(type(exc).__name__)
        observability.metrics.observe(
            request.method, observability.route_template(request), 500, elapsed
        )
        logger.exception(
            "request failed",
            extra={
                "method": request.method,
                "route": observability.route_template(request),
                "duration_ms": round(elapsed * 1000, 2),
            },
        )
        raise
    finally:
        observability.request_id_var.reset(token)

    elapsed = time.perf_counter() - started
    route = observability.route_template(request)
    observability.metrics.observe(request.method, route, response.status_code, elapsed)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request",
        extra={
            # Deliberately the templated route and never the query string: `q`
            # on address search is a user's home address.
            "method": request.method,
            "route": route,
            "status": response.status_code,
            "duration_ms": round(elapsed * 1000, 2),
        },
    )
    return response


@app.get("/metrics", tags=["meta"], include_in_schema=False)
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus exposition. Per-process, so scrape each worker as its own target."""
    return PlainTextResponse(
        observability.metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Liveness: the process is up. Cheap enough for a load balancer to poll."""
    return {"status": "ok", "service": settings.app_name, "env": settings.environment}


@app.get("/health/ready", tags=["meta"])
async def readiness() -> JSONResponse:
    """Readiness: the dependencies this process needs are actually reachable.

    The old check returned ok unconditionally, so a container with a dead
    database still reported healthy and kept taking traffic.
    """
    checks: dict[str, str] = {}

    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report any failure shape
        checks["database"] = f"error: {type(exc).__name__}"

    # The OTP store is Redis in production and an in-process dict in dev; either
    # way a failing round trip means sign-in is broken.
    try:
        otp_store._store.set("health:probe", "1", 10)
        checks["otp_store"] = (
            "ok" if otp_store._store.get("health:probe") == "1" else "error: readback"
        )
    except Exception as exc:  # noqa: BLE001
        checks["otp_store"] = f"error: {type(exc).__name__}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "degraded", "checks": checks},
    )
