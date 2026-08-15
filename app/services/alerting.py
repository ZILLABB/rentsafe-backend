"""Operational alerts for jobs that run without anybody watching.

Backups have a failure mode that ordinary error reporting misses entirely. A
webhook fired when the job fails tells you nothing when the job **never ran** —
cron was removed, the container stopped being scheduled, the disk filled so the
script died before it could report. Those are silent, and silence looks exactly
like success.

So there are two channels, and they answer different questions:

  heartbeat   Pinged only on success. An external monitor expects it on a
              schedule and alerts when it stops arriving. This is what catches
              "the job never ran" — the case you cannot detect from inside.
  webhook     Posted on failure, with the reason. This is what tells you *why*
              on the occasions the job did run and did fail.

Both are optional and provider-agnostic. Nothing here needs an account to test,
and neither can break the job it is reporting on: an alerting call that raises
would turn a failed backup into a crashed backup, losing the diagnostic.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Short: this runs at the end of a job that has already done its work, and a
# hung alerting call should not hold a nightly cron open indefinitely.
TIMEOUT_S = 10.0


def heartbeat(job: str) -> bool:
    """Tell an external monitor the job completed. Returns whether it landed.

    Deliberately only called on success. The whole value of a dead-man's switch
    is that *not* pinging is the signal — pinging regardless of outcome would
    reduce it to an expensive no-op.

    Works with any URL that accepts a GET: Healthchecks.io, Better Stack,
    Cronitor, or a self-hosted equivalent.
    """
    url = settings.backup_heartbeat_url
    if not url:
        return False
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            client.get(url).raise_for_status()
        logger.info("Heartbeat sent for %s", job)
        return True
    except Exception as exc:  # noqa: BLE001 - alerting must never raise
        # A missed heartbeat is itself an alert, so failing to send one is not
        # worth escalating here — the monitor will notice.
        logger.warning("Heartbeat for %s failed: %s", job, type(exc).__name__)
        return False


def alert(job: str, message: str, *, detail: str = "") -> bool:
    """Report that a job failed. Returns whether the alert was delivered.

    The payload carries both a plain ``text`` field and structured keys, so it
    works unmodified with Slack and Discord incoming webhooks — which render
    ``text``/``content`` — as well as with anything that parses JSON.
    """
    delivered = False
    body = {
        # Slack reads `text`; Discord reads `content`. Sending both means one
        # payload works with either without a provider setting.
        "text": f"[RentSafe] {job}: {message}",
        "content": f"[RentSafe] {job}: {message}",
        "job": job,
        "status": "failed",
        "message": message,
        "detail": detail,
        "environment": settings.environment,
    }

    if settings.alert_webhook_url:
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                client.post(settings.alert_webhook_url, json=body).raise_for_status()
            delivered = True
        except Exception as exc:  # noqa: BLE001 - alerting must never raise
            logger.error("Alert webhook failed: %s", type(exc).__name__)

    if settings.sentry_dsn:
        try:
            import sentry_sdk

            sentry_sdk.capture_message(
                f"{job}: {message}", level="error"
            )
            delivered = True
        except Exception as exc:  # noqa: BLE001
            logger.error("Sentry capture failed: %s", type(exc).__name__)

    # Always logged, whether or not anything is configured. In production these
    # are JSON lines carrying the request id, so a failure is recoverable from
    # the logs even with no alerting wired at all.
    logger.error("%s failed: %s %s", job, message, detail)
    return delivered
