"""SMS delivery for one-time codes.

Until this existed, production was unusable: the OTP endpoint generated a code,
stored it, returned ``{"sent": true}`` and delivered nothing, because the echo
path is gated on ``debug``. Every user would have been locked out at sign-in
while the API cheerfully reported success.

Termii is the provider the project already declared. The interface is small
enough to swap for Africa's Talking or Twilio if the commercial answer changes.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

TERMII_URL = "https://api.ng.termii.com/api/sms/send"

# Termii requires a pre-registered alphanumeric sender ID in Nigeria.
SENDER_ID = "RentSafe"


class SMSError(RuntimeError):
    """Delivery failed. The caller must not pretend the code was sent."""


class SMSProvider:
    """Base interface."""

    async def send(self, phone: str, message: str) -> None:  # pragma: no cover
        raise NotImplementedError


class ConsoleSMS(SMSProvider):
    """Development sink: log the message instead of paying for it.

    Only selectable when ``debug`` is on — the config guard blocks production
    from silently falling back to this and losing every login.
    """

    async def send(self, phone: str, message: str) -> None:
        logger.info("[dev-sms] to %s: %s", phone, message)


class TermiiSMS(SMSProvider):
    """Live delivery via Termii."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def send(self, phone: str, message: str) -> None:
        payload = {
            "to": phone.lstrip("+"),
            "from": SENDER_ID,
            "sms": message,
            "type": "plain",
            "channel": "dnd",  # reaches numbers on Nigeria's do-not-disturb list
            "api_key": self._api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(TERMII_URL, json=payload)
        except httpx.HTTPError as exc:
            raise SMSError("Could not reach the SMS provider.") from exc

        if r.status_code >= 400:
            # Never log the body unredacted — it echoes the message, which
            # contains the one-time code.
            logger.error("Termii rejected a send: HTTP %s", r.status_code)
            raise SMSError("The SMS provider rejected that number.")

        body = r.json() if r.content else {}
        if isinstance(body, dict) and body.get("code") not in (None, "ok", 200):
            logger.error("Termii send failed: %s", body.get("message"))
            raise SMSError("The SMS provider could not deliver to that number.")


def build_provider() -> SMSProvider:
    """Pick a provider from config.

    Production without a key is a hard failure rather than a silent downgrade:
    a deployment that cannot send codes cannot sign anybody in, and finding
    that out from user complaints is worse than finding it out at boot.
    """
    if settings.termii_api_key:
        return TermiiSMS(settings.termii_api_key)
    if settings.debug:
        return ConsoleSMS()
    raise RuntimeError(
        "TERMII_API_KEY is required outside development — without it no user "
        "can receive a sign-in code."
    )


_provider: SMSProvider | None = None


def get_provider() -> SMSProvider:
    global _provider
    if _provider is None:
        _provider = build_provider()
    return _provider


async def send_otp(phone: str, code: str) -> None:
    """Deliver a one-time code. Raises SMSError if it did not go out."""
    await get_provider().send(
        phone,
        f"{code} is your RentSafe verification code. It expires in 5 minutes. "
        f"RentSafe will never ask you for this code.",
    )
