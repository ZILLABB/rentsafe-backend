"""Launch blockers: SMS delivery, right of reply, and NDPR data rights."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Review, SavedProperty, User
from app.services import sms
from tests.conftest import ADMIN_PHONE, OTHER_PHONE, TENANT_PHONE, auth, login
from tests.test_api_moderation import (  # noqa: F401 — fixtures used by name
    PROPERTY_ID,
    _payload,
    property_row,
)

# ------------------------------------------------------------------- SMS

@pytest.fixture(autouse=True)
def sms_sink(monkeypatch):
    sent = []

    class Recording(sms.SMSProvider):
        async def send(self, phone, message):
            sent.append((phone, message))

    monkeypatch.setattr(sms, "_provider", Recording())
    return sent


async def test_requesting_a_code_actually_sends_it(client, users, sms_sink):
    """Production previously returned sent=true and delivered nothing."""
    r = await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})
    assert r.status_code == 200
    assert len(sms_sink) == 1

    phone, message = sms_sink[0]
    assert phone == "+2348012345678"
    assert r.json()["dev_code"] in message
    assert "never ask you for this code" in message


async def test_delivery_failure_is_reported_not_swallowed(client, users, monkeypatch):
    """Claiming success for an undelivered code locks the user out silently."""

    class Broken(sms.SMSProvider):
        async def send(self, phone, message):
            raise sms.SMSError("carrier rejected")

    monkeypatch.setattr(sms, "_provider", Broken())

    r = await client.post("/auth/otp/request", json={"phone": TENANT_PHONE})
    assert r.status_code == 502
    assert "send" in r.json()["detail"].lower()


def test_production_without_an_sms_key_refuses_to_start(monkeypatch):
    """A deploy that cannot send codes cannot sign anyone in."""
    monkeypatch.setattr(sms.settings, "termii_api_key", "")
    monkeypatch.setattr(sms.settings, "debug", False)
    with pytest.raises(RuntimeError, match="TERMII_API_KEY"):
        sms.build_provider()


def test_development_falls_back_to_the_console(monkeypatch):
    monkeypatch.setattr(sms.settings, "termii_api_key", "")
    monkeypatch.setattr(sms.settings, "debug", True)
    assert isinstance(sms.build_provider(), sms.ConsoleSMS)


# ---------------------------------------------------------- right of reply

async def _approved_review(client) -> tuple[int, str]:
    """Publish a review and hand back its id plus a live admin token.

    The admin token is returned rather than re-fetched: signing in twice with
    the same number inside the resend cooldown is a 429, which is the throttle
    working correctly.
    """
    token = await login(client, TENANT_PHONE)
    rid = (
        await client.post("/reviews", json=_payload(), headers=auth(token))
    ).json()["review_id"]
    admin = await login(client, ADMIN_PHONE)
    await client.patch(
        f"/admin/moderation/reviews/{rid}",
        json={"action": "approve"},
        headers=auth(admin),
    )
    return rid, admin


async def test_a_stranger_cannot_reply_in_a_landlords_name(
    client, users, property_row
):
    rid, _ = await _approved_review(client)
    token = await login(client, OTHER_PHONE)
    r = await client.post(
        f"/reviews/{rid}/response",
        json={"text": "This tenant is lying about everything they wrote here."},
        headers=auth(token),
    )
    assert r.status_code == 403


async def test_replies_to_unpublished_reviews_are_not_possible(
    client, users, property_row
):
    """Replying would reveal a review the queue is still holding."""
    token = await login(client, TENANT_PHONE)
    rid = (
        await client.post("/reviews", json=_payload(), headers=auth(token))
    ).json()["review_id"]
    admin = await login(client, ADMIN_PHONE)
    r = await client.post(
        f"/reviews/{rid}/response",
        json={"text": "Answering something nobody outside can see yet."},
        headers=auth(admin),
    )
    assert r.status_code == 404


async def test_a_reply_appears_beside_the_review(client, users, property_row):
    rid, admin = await _approved_review(client)

    r = await client.post(
        f"/reviews/{rid}/response",
        json={
            "text": (
                "The drainage was upgraded in 2025 and renewal notices now go "
                "out three months ahead."
            ),
            "author_role": "landlord",
        },
        headers=auth(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"

    public = (await client.get(f"/properties/{PROPERTY_ID}/reviews")).json()
    assert public[0]["owner_response"]["from"] == "landlord"
    assert "drainage was upgraded" in public[0]["owner_response"]["text"]


async def test_a_defamatory_reply_is_held_like_a_review(client, users, property_row):
    """A landlord can defame a tenant back."""
    rid, admin = await _approved_review(client)
    r = await client.post(
        f"/reviews/{rid}/response",
        json={"text": "This tenant is a fraudster who never paid a single naira."},
        headers=auth(admin),
    )
    assert r.json()["status"] == "flagged"


async def test_a_review_cannot_be_answered_twice(client, users, property_row):
    rid, admin = await _approved_review(client)
    body = {"text": "A considered and sufficiently long first reply goes here."}
    assert (
        await client.post(f"/reviews/{rid}/response", json=body, headers=auth(admin))
    ).status_code == 200
    assert (
        await client.post(f"/reviews/{rid}/response", json=body, headers=auth(admin))
    ).status_code == 409


# ------------------------------------------------------------- NDPR rights

async def test_users_can_export_everything_held_about_them(
    client, users, property_row
):
    token = await login(client, TENANT_PHONE)
    await client.post("/reviews", json=_payload(), headers=auth(token))
    await client.put(f"/properties/{PROPERTY_ID}/save", headers=auth(token))

    data = (await client.get("/users/me/export", headers=auth(token))).json()
    assert data["account"]["phone_last4"] == "5678"
    assert len(data["reviews"]) == 1
    assert data["saved_properties"] == [PROPERTY_ID]
    # The number itself is unrecoverable, so it is not in the export.
    assert "phone_hash" not in str(data)


async def test_export_requires_sign_in(client, users):
    assert (await client.get("/users/me/export")).status_code == 401


async def test_deleting_an_account_anonymises_rather_than_erases_by_default(
    client, users, property_row, session_factory
):
    """A record a landlord can pressure a tenant into deleting is worth little."""
    token = await login(client, TENANT_PHONE)
    rid = (
        await client.post("/reviews", json=_payload(), headers=auth(token))
    ).json()["review_id"]
    await client.put(f"/properties/{PROPERTY_ID}/save", headers=auth(token))

    r = await client.post(
        "/users/me/delete", json={"confirm": "DELETE"}, headers=auth(token)
    )
    assert r.status_code == 200
    assert r.json()["reviews_anonymised"] == 1

    async with session_factory() as s:
        review = (await s.execute(select(Review).where(Review.id == rid))).scalar_one()
        assert review.is_anonymous is True
        assert review.display_name == "Former tenant"
        assert review.moderation_status != "rejected"  # the record survives

        user = (
            await s.execute(select(User).where(User.id == users["tenant"].id))
        ).scalar_one()
        assert user.is_active is False
        assert user.phone_last4 is None
        assert user.phone_hash.startswith("deleted:")

        assert (await s.execute(select(SavedProperty))).scalars().all() == []


async def test_deletion_can_also_withdraw_the_reviews(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    await client.post("/reviews", json=_payload(), headers=auth(token))

    r = await client.post(
        "/users/me/delete",
        json={"confirm": "DELETE", "remove_reviews": True},
        headers=auth(token),
    )
    assert r.json()["reviews_withdrawn"] == 1
    assert (await client.get(f"/properties/{PROPERTY_ID}/reviews")).json() == []


async def test_deletion_needs_explicit_confirmation(client, users):
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        "/users/me/delete", json={"confirm": "yes"}, headers=auth(token)
    )
    assert r.status_code == 422


async def test_a_deleted_account_cannot_be_used(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    await client.post(
        "/users/me/delete", json={"confirm": "DELETE"}, headers=auth(token)
    )
    # is_active False -> the token no longer resolves to a user.
    assert (await client.get("/users/me", headers=auth(token))).status_code == 401
