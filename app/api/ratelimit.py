"""Per-IP rate limiting for endpoints that anonymous callers can reach.

Most write endpoints sit behind phone verification, which is its own throttle.
Two do not, by design, and both were abusable:

  * ``POST /properties/identify`` registers a building. Requiring an account
    first would mean verifying your phone before you can even say where you
    live, which breaks the one funnel that matters. So it stays open and gets
    a quota instead.
  * ``GET /places/search`` reaches Nominatim and writes the response to disk.
    Unlimited, that is both a disk-fill and a way to get our IP banned from a
    free service other people depend on.

Counters live in the OTP store, which is Redis in production and an in-process
dict in development — so this is fleet-wide wherever it needs to be.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.services import otp_store

settings = get_settings()


def client_ip(request: Request) -> str:
    """The caller's address, trusting only as much of X-Forwarded-For as we should.

    Each proxy *appends* the address it saw, so the rightmost entries are the
    ones written by infrastructure we control and the leftmost is whatever the
    client sent. Taking ``split(",")[0]`` — the previous behaviour — therefore
    let any caller pick their own identity by sending the header themselves,
    which defeats every per-IP limit here and the OTP quota that guards the SMS
    bill.

    ``trusted_proxy_hops`` says how many entries to peel off the right. At 0 the
    header is ignored entirely, which is correct when nothing is in front of the
    app: no configuration can then be silently wrong in the permissive
    direction.
    """
    socket_ip = request.client.host if request.client else "unknown"
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        return socket_ip

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return socket_ip

    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return socket_ip
    # The socket address counts as the last hop, so N trusted proxies means the
    # Nth entry from the right is the furthest we can believe.
    index = max(0, len(chain) - hops)
    return chain[index] if index < len(chain) else chain[0]


# Kept as the private name the tests and older call sites use.
_client_ip = client_ip


def rate_limit(*, key: str, limit: int, window_s: int, message: str):
    """Build a FastAPI dependency enforcing `limit` requests per `window_s`."""

    async def dependency(request: Request) -> None:
        bucket = f"rl:{key}:{window_s}:{client_ip(request)}"
        used = otp_store._store.incr(bucket, window_s)
        if used > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=message,
                headers={"Retry-After": str(window_s)},
            )

    return dependency


# Registering a building is a once-in-a-tenancy action; ten an hour is
# generous for a household and useless for a spammer.
registration_limit = rate_limit(
    key="identify",
    limit=10,
    window_s=3600,
    message="Too many properties registered from this network. Try again later.",
)

# Address search is interactive and debounced client-side, so a real session
# spends a handful of these.
search_limit = rate_limit(
    key="places",
    limit=60,
    window_s=3600,
    message="Too many address searches from this network. Try again shortly.",
)
