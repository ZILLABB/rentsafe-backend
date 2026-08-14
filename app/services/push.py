"""Web Push delivery for area watches.

Watches personalised the in-app feed and drove the unread badge, but nothing
ever reached a phone. That made the feature half-useful in exactly the case it
exists for: a flood report or a damning new review about the street you are
about to sign a lease on is only actionable if it finds you.

Design notes:

* **Web Push, not SMS.** SMS costs money per message and this is a
  notification, not an authentication factor. Push is free, revocable by the
  user in one tap, and never passes through our SMS provider.
* **Optional.** With no VAPID keys configured everything here degrades to a
  no-op and the UI says push is unavailable. It must never be a boot failure —
  unlike SMS, nobody is locked out of the product without it.
* **Best-effort.** A push that fails must never fail the request that triggered
  it. A tenant's review is published whether or not the watchers' phones buzz.

Payloads deliberately carry no review text. A notification is rendered by the
operating system, cached by the push service, and shown on a lock screen —
outside every moderation and privacy control this app has. "New review in Yaba"
is enough to bring someone into the app, where those controls apply.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db.models import AreaWatch, PushSubscription

logger = logging.getLogger(__name__)
settings = get_settings()

# A push service that answers 404 or 410 is gone for good — the browser was
# uninstalled or permission was revoked. Anything else (a 500, a timeout) may be
# transient, so those are counted and only dropped after repeated failure rather
# than on the first blip.
MAX_FAILURES = 5


def is_configured() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


def public_key() -> str:
    """The application server key a browser needs in order to subscribe."""
    return settings.vapid_public_key


def _send_one(subscription: dict, payload: str) -> int:
    """Blocking send. Returns the HTTP status, or 0 when the library is absent.

    pywebpush is synchronous and does elliptic-curve work per message, so every
    caller runs this in a thread.
    """
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush is not installed; push notifications disabled")
        return 0

    try:
        response = webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=settings.vapid_private_key,
            # The spec requires a contact so a push service can reach us about
            # abuse. Omitting it is how a sender gets rate-limited.
            vapid_claims={"sub": settings.vapid_subject},
            timeout=10,
        )
        return int(getattr(response, "status_code", 201))
    except WebPushException as exc:
        return int(getattr(getattr(exc, "response", None), "status_code", 500))


async def notify_area(
    session: AsyncSession,
    *,
    area_code: str,
    kind: str,
    title: str,
    body: str,
    url: str = "/alerts",
    exclude_user_id: int | None = None,
) -> int:
    """Push to everyone watching ``area_code``. Returns how many were delivered.

    ``kind`` is matched against the per-watch notify flags, so someone watching
    Yaba for floods only is not told about every new review there.
    """
    if not is_configured():
        return 0

    flag = {
        "review": AreaWatch.notify_reviews,
        "flood": AreaWatch.notify_floods,
        "agent_flag": AreaWatch.notify_agent_flags,
    }.get(kind)
    if flag is None:
        return 0

    stmt = (
        select(PushSubscription)
        .join(AreaWatch, AreaWatch.user_id == PushSubscription.user_id)
        .where(AreaWatch.area_code == area_code.upper(), flag.is_(True))
    )
    if exclude_user_id is not None:
        # Don't buzz someone's own phone about their own review being published.
        stmt = stmt.where(PushSubscription.user_id != exclude_user_id)

    subs = (await session.execute(stmt)).scalars().all()
    if not subs:
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url, "kind": kind})

    sent = 0
    dead: list[PushSubscription] = []
    for sub in subs:
        info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        status = await run_in_threadpool(_send_one, info, payload)
        if status in (404, 410):
            dead.append(sub)
        elif status == 0 or status >= 400:
            sub.failure_count += 1
            if sub.failure_count >= MAX_FAILURES:
                dead.append(sub)
        else:
            sent += 1
            sub.failure_count = 0

    for sub in dead:
        await session.delete(sub)
    await session.commit()

    return sent


async def notify_area_safe(session: AsyncSession, **kwargs) -> int:
    """``notify_area`` that can never break its caller.

    Every call site is a user action that has already succeeded — a review
    approved, a flood reported. Failing that action because a push service timed
    out would be absurd.
    """
    try:
        return await notify_area(session, **kwargs)
    except Exception:
        logger.warning("Push notification failed", exc_info=True)
        return 0
