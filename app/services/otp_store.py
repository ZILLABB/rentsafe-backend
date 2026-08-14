"""Short-lived OTP storage and throttling.

Production uses Redis (Section XIX) so the state is shared across uvicorn
workers — an in-process dict silently breaks verification whenever the verify
request lands on a different worker than the request did. When ``REDIS_URL``
isn't reachable this falls back to an in-process store with identical TTL
semantics, which is fine for single-process local dev and nothing else.

Only the HMAC of the code is stored — never the plaintext (see
``app.core.security.hash_otp``).
"""

from __future__ import annotations

import logging
import time

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

OTP_TTL_SECONDS = 300  # 5 minutes


class _MemoryStore:
    """Dev fallback. Same surface as the Redis client methods used below."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if time.monotonic() > expires:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ttl: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def incr(self, key: str, ttl: int) -> int:
        """Increment a counter, creating it with the given TTL on first use."""
        current = self.get(key)
        nxt = int(current or 0) + 1
        # Preserve the original expiry so the window doesn't slide forward.
        expires = self._data[key][1] if key in self._data else time.monotonic() + ttl
        self._data[key] = (str(nxt), expires)
        return nxt


class _RedisStore:
    def __init__(self, client) -> None:
        self._r = client

    def get(self, key: str) -> str | None:
        v = self._r.get(key)
        return v.decode() if isinstance(v, bytes) else v

    def set(self, key: str, value: str, ttl: int) -> None:
        self._r.set(key, value, ex=ttl)

    def delete(self, key: str) -> None:
        self._r.delete(key)

    def incr(self, key: str, ttl: int) -> int:
        n = self._r.incr(key)
        if n == 1:
            self._r.expire(key, ttl)
        return int(n)


def _build_store():
    """Prefer Redis; fall back to memory with a loud warning."""
    try:
        import redis  # imported lazily so local dev needn't install it

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        return _RedisStore(client)
    except Exception as exc:  # redis missing, down, or misconfigured
        if not settings.debug:
            # In production this is a correctness bug waiting to happen.
            raise RuntimeError(
                f"OTP store requires Redis outside development: {exc}"
            ) from exc
        logger.warning("Redis unavailable (%s) — using in-process OTP store", exc)
        return _MemoryStore()


_store = _build_store()


def _key(phone_hash: str) -> str:
    return f"otp:{phone_hash}"


def _attempts_key(phone_hash: str) -> str:
    return f"otp:attempts:{phone_hash}"


def _cooldown_key(phone_hash: str) -> str:
    return f"otp:cooldown:{phone_hash}"


def _quota_key(scope: str, ident: str, window: str) -> str:
    return f"otp:quota:{window}:{scope}:{ident}"


class OTPThrottled(Exception):
    """Raised when a caller exceeds a request quota or the resend cooldown."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def check_request_quota(phone_hash: str, client_ip: str | None) -> None:
    """Enforce resend cooldown plus per-phone and per-IP request quotas.

    Without this, ``/auth/otp/request`` is an unmetered SMS bill pointed at
    arbitrary Nigerian numbers.
    """
    if _store.get(_cooldown_key(phone_hash)):
        raise OTPThrottled(
            "A code was just sent. Wait a moment before requesting another.",
            settings.otp_resend_cooldown_s,
        )

    hourly = _store.incr(_quota_key("phone", phone_hash, "1h"), 3600)
    if hourly > settings.otp_requests_per_hour:
        raise OTPThrottled("Too many codes requested for this number. Try again later.", 3600)

    daily = _store.incr(_quota_key("phone", phone_hash, "24h"), 86400)
    if daily > settings.otp_requests_per_day:
        raise OTPThrottled("Daily limit reached for this number. Try again tomorrow.", 86400)

    if client_ip:
        # IP quota is deliberately looser — shared NAT is the norm in Lagos.
        ip_hourly = _store.incr(_quota_key("ip", client_ip, "1h"), 3600)
        if ip_hourly > settings.otp_requests_per_hour * 5:
            raise OTPThrottled("Too many requests from this network. Try again later.", 3600)


def save_otp(phone_hash: str, otp_hash: str) -> None:
    _store.set(_key(phone_hash), otp_hash, OTP_TTL_SECONDS)
    _store.delete(_attempts_key(phone_hash))
    _store.set(_cooldown_key(phone_hash), "1", settings.otp_resend_cooldown_s)


def load_otp(phone_hash: str) -> str | None:
    return _store.get(_key(phone_hash))


def register_failed_attempt(phone_hash: str) -> bool:
    """Count a wrong code. Returns True when the code has been burned.

    A 6-digit code with a 5-minute window is brute-forceable at a few thousand
    guesses; capping attempts is what makes the length adequate.
    """
    attempts = _store.incr(_attempts_key(phone_hash), OTP_TTL_SECONDS)
    if attempts >= settings.otp_max_attempts:
        clear_otp(phone_hash)
        return True
    return False


def clear_otp(phone_hash: str) -> None:
    _store.delete(_key(phone_hash))
    _store.delete(_attempts_key(phone_hash))
