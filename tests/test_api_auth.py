"""Route tests for authentication, throttling and role enforcement."""

from __future__ import annotations

import pytest

from app.config import get_settings
from tests.conftest import ADMIN_PHONE, TENANT_PHONE, auth, login

settings = get_settings()


async def test_otp_round_trip_issues_a_usable_token(client, users):
    token = await login(client, TENANT_PHONE)
    # A token alone isn't admin — see the RBAC test below.
    r = await client.get("/admin/moderation/reviews", headers=auth(token))
    assert r.status_code == 403


async def test_admin_phone_reaches_the_moderation_queue(client, users):
    """The flow the README documents for signing into /admin."""
    token = await login(client, ADMIN_PHONE)
    r = await client.get("/admin/moderation/reviews", headers=auth(token))
    assert r.status_code == 200
    assert r.json() == []


async def test_moderation_queue_rejects_anonymous_callers(client, users):
    # 401 (no credential) rather than 403 (credential, wrong role).
    assert (await client.get("/admin/moderation/reviews")).status_code == 401


async def test_tenant_cannot_moderate(client, users):
    token = await login(client, TENANT_PHONE)
    r = await client.patch(
        "/admin/moderation/reviews/1", json={"action": "approve"}, headers=auth(token)
    )
    assert r.status_code == 403


async def test_resend_cooldown_blocks_immediate_repeat(client, users):
    first = await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})
    assert first.status_code == 200

    second = await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})
    assert second.status_code == 429
    assert "Retry-After" in second.headers


async def test_hourly_quota_caps_requests_per_phone(client, users, monkeypatch):
    """Without this, /auth/otp/request is an unmetered SMS bill."""
    monkeypatch.setattr(settings, "otp_resend_cooldown_s", 0)

    codes = [
        (await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})).status_code
        for _ in range(settings.otp_requests_per_hour + 2)
    ]
    assert codes[: settings.otp_requests_per_hour] == [200] * settings.otp_requests_per_hour
    assert codes[settings.otp_requests_per_hour :] == [429, 429]


async def test_wrong_codes_burn_the_otp(client, users):
    """A 6-digit code is only adequate if guesses are capped."""
    await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})

    for _ in range(settings.otp_max_attempts - 1):
        r = await client.post(
            "/auth/otp/verify", json={"phone": TENANT_PHONE, "code": "000000"}
        )
        assert r.status_code == 401

    burned = await client.post(
        "/auth/otp/verify", json={"phone": TENANT_PHONE, "code": "000000"}
    )
    assert "too many" in burned.json()["detail"]


async def test_expired_or_absent_code_is_rejected(client, users):
    r = await client.post(
        "/auth/otp/verify", json={"phone": TENANT_PHONE, "code": "123456"}
    )
    assert r.status_code == 401


async def test_malformed_phone_is_rejected(client, users):
    r = await client.post("/auth/otp/request", json={"phone": "12345"})
    assert r.status_code == 422


async def test_refresh_token_cannot_be_used_as_access_token(client, users):
    r = await client.post("/auth/otp/request", json={"phone": ADMIN_PHONE})
    code = r.json()["dev_code"]
    tokens = (
        await client.post(
            "/auth/otp/verify", json={"phone": ADMIN_PHONE, "code": code}
        )
    ).json()

    r = await client.get(
        "/admin/moderation/reviews", headers=auth(tokens["refresh_token"])
    )
    assert r.status_code == 401


async def test_refresh_endpoint_rotates_tokens(client, users):
    r = await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})
    code = r.json()["dev_code"]
    tokens = (
        await client.post(
            "/auth/otp/verify", json={"phone": TENANT_PHONE, "code": code}
        )
    ).json()

    refreshed = await client.post(
        "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["user_id"] == tokens["user_id"]


async def test_garbage_token_is_rejected(client, users):
    r = await client.get("/admin/moderation/reviews", headers=auth("not-a-jwt"))
    assert r.status_code == 401


@pytest.mark.parametrize("env", ["production", "staging"])
def test_production_config_rejects_dev_secrets(env, monkeypatch):
    """The guard that stops placeholder secrets reaching a deployed environment."""
    from app.config import Settings

    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("JWT_SECRET", "dev-secret-change-me")
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_production_config_rejects_debug(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("JWT_SECRET", "a" * 40)
    with pytest.raises(ValueError, match="DEBUG"):
        Settings(_env_file=None)
