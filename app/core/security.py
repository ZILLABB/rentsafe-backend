"""Security primitives — phone hashing, OTP codes, JWTs (Section XV).

Phone numbers are stored only as SHA-256 hashes, never plaintext. We keep the
hashing + normalisation here (stdlib, testable). JWT encode/decode uses python-jose
at runtime.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets

from app.config import get_settings

settings = get_settings()

_NG_DIGITS = re.compile(r"\D")

# PBKDF2 rounds for phone hashing. Phone lookup happens once per login, so this
# can be slow enough to matter to an attacker without mattering to a user.
PHONE_HASH_ROUNDS = 200_000


def normalise_phone(raw: str) -> str:
    """Normalise a Nigerian phone number to E.164 (+234XXXXXXXXXX).

    Accepts the common local forms: 08031234567, 8031234567, 234803..., +234803...
    """
    digits = _NG_DIGITS.sub("", raw)
    if digits.startswith("234"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"not a valid Nigerian phone number: {raw!r}")
    return f"+234{digits}"


def hash_phone(raw: str) -> str:
    """Hash of the normalised phone, peppered with ``phone_hash_pepper``.

    Uses PBKDF2 rather than a bare SHA-256: the Nigerian mobile space is only
    ~10 digits wide, so a plain digest would be trivially enumerable if the
    users table ever leaked.

    The pepper is deliberately *not* ``jwt_secret`` — this hash is the only way
    back to a user row (we never store the plaintext number), so it must survive
    JWT-secret rotation.
    """
    phone = normalise_phone(raw)
    return hashlib.pbkdf2_hmac(
        "sha256", phone.encode(), settings.phone_hash_pepper.encode(), PHONE_HASH_ROUNDS
    ).hex()


def phone_last4(raw: str) -> str:
    return normalise_phone(raw)[-4:]


def generate_otp(length: int = 6) -> str:
    """Cryptographically-random numeric OTP."""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_otp(phone_hash: str, code: str) -> str:
    """HMAC of an OTP so we never store the plaintext code in Redis."""
    return hmac.new(
        settings.jwt_secret.encode(), f"{phone_hash}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def verify_otp(phone_hash: str, code: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(phone_hash, code), stored_hash)


# --- JWT (runtime; requires python-jose) ------------------------------------
ACCESS_TTL = dt.timedelta(minutes=15)
REFRESH_TTL = dt.timedelta(days=30)


def create_token(user_id: int, *, refresh: bool = False) -> str:
    from jose import jwt  # imported lazily so core tests don't need the dep

    now = dt.datetime.now(dt.UTC)
    ttl = REFRESH_TTL if refresh else ACCESS_TTL
    payload = {
        "sub": str(user_id),
        "type": "refresh" if refresh else "access",
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    from jose import jwt

    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
