"""Changing the number on an account.

The phone hash is the only route back to a user row — plaintext numbers are
never stored — so before this, losing a number meant losing the account, every
review written from it, and the tenancy history that gives those reviews their
weight. Nigerian numbers change hands often enough that this is a routine event,
not an edge case.

The risk being managed is account takeover: whoever controls the phone hash
controls the account, so a flaw here is worse than the problem it solves.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core import security
from app.db.models import User
from tests.conftest import ADMIN_PHONE, OTHER_PHONE, TENANT_PHONE, auth, login

NEW_PHONE = "08055512345"


def code_sent_to(sent: list[tuple[str, str]], phone: str) -> str:
    """The code delivered to one specific number.

    Signing in also sends an OTP, so a capture list holds more than the one
    under test. Picking by index silently verifies the login code against the
    new number's hash and fails for a reason that has nothing to do with the
    feature.
    """
    wanted = security.normalise_phone(phone)
    for number, code in sent:
        if number == wanted:
            return code
    raise AssertionError(f"no code was sent to {wanted}: {sent!r}")


async def test_a_signed_in_user_can_move_to_a_new_number(client, users, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send(phone, code):
        sent.append((phone, code))

    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api.sms, "send_otp", fake_send)

    token = await login(client, TENANT_PHONE)
    start = await client.post(
        "/users/me/phone/start", json={"new_phone": NEW_PHONE}, headers=auth(token)
    )
    assert start.status_code == 200
    assert code_sent_to(sent, NEW_PHONE)

    confirm = await client.post(
        "/users/me/phone/confirm",
        json={"new_phone": NEW_PHONE, "code": code_sent_to(sent, NEW_PHONE)},
        headers=auth(token),
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["phone_last4"] == security.phone_last4(NEW_PHONE)


async def test_the_old_number_stops_working_and_the_new_one_starts(
    client, users, session_factory, monkeypatch
):
    """The point of the feature: the account follows the new number."""
    sent: list[tuple[str, str]] = []

    async def fake_send(phone, code):
        sent.append((phone, code))

    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api.sms, "send_otp", fake_send)

    token = await login(client, TENANT_PHONE)
    original_id = (await client.get("/users/me", headers=auth(token))).json()["id"]

    await client.post(
        "/users/me/phone/start", json={"new_phone": NEW_PHONE}, headers=auth(token)
    )
    await client.post(
        "/users/me/phone/confirm",
        json={"new_phone": NEW_PHONE, "code": code_sent_to(sent, NEW_PHONE)},
        headers=auth(token),
    )

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(User).where(User.phone_hash == security.hash_phone(TENANT_PHONE))
            )
        ).scalars().all()
        assert rows == [], "the old number still resolves to an account"

        moved = (
            await s.execute(
                select(User).where(User.phone_hash == security.hash_phone(NEW_PHONE))
            )
        ).scalar_one()
        # Same row, not a new account: the reviews come with it.
        assert moved.id == original_id


async def test_changing_a_number_requires_being_signed_in(client, users):
    """Proof of the *old* number is the session token itself.

    Without this the endpoint would let anyone move any account to a number
    they control, which is account takeover with extra steps.
    """
    r = await client.post("/users/me/phone/start", json={"new_phone": NEW_PHONE})
    assert r.status_code in (401, 403)

    r = await client.post(
        "/users/me/phone/confirm", json={"new_phone": NEW_PHONE, "code": "123456"}
    )
    assert r.status_code in (401, 403)


async def test_a_wrong_code_does_not_move_the_account(client, users, monkeypatch):
    async def fake_send(phone, code):
        return None

    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api.sms, "send_otp", fake_send)

    token = await login(client, TENANT_PHONE)
    await client.post(
        "/users/me/phone/start", json={"new_phone": NEW_PHONE}, headers=auth(token)
    )
    r = await client.post(
        "/users/me/phone/confirm",
        json={"new_phone": NEW_PHONE, "code": "000000"},
        headers=auth(token),
    )
    assert r.status_code == 400
    # And the account still answers to the original number.
    me = await client.get("/users/me", headers=auth(token))
    assert me.json()["phone_last4"] == security.phone_last4(TENANT_PHONE)


async def test_cannot_take_over_a_number_that_belongs_to_someone_else(
    client, users, monkeypatch
):
    """Two accounts on one phone hash is a unique-constraint 500 at best.

    At worst, if it succeeded, it would be a way to seize another tenant's
    review history.
    """
    sent: list[tuple[str, str]] = []

    async def fake_send(phone, code):
        sent.append((phone, code))

    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api.sms, "send_otp", fake_send)

    token = await login(client, TENANT_PHONE)
    r = await client.post(
        "/users/me/phone/start",
        json={"new_phone": OTHER_PHONE},  # already a registered account
        headers=auth(token),
    )
    # Deliberately shaped like a success so this is not a way to enumerate which
    # numbers are registered...
    assert r.status_code == 200
    # ...but no code is ever sent to a number we cannot move to. (The login OTP
    # is in `sent` too, hence checking for this number specifically.)
    assert not any(n == security.normalise_phone(OTHER_PHONE) for n, _ in sent)


async def test_confirm_rejects_a_number_claimed_after_start(
    client, users, session_factory, monkeypatch
):
    """The check at start is not enough — the number can be claimed in between."""
    sent: list[tuple[str, str]] = []

    async def fake_send(phone, code):
        sent.append((phone, code))

    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api.sms, "send_otp", fake_send)

    token = await login(client, TENANT_PHONE)
    await client.post(
        "/users/me/phone/start", json={"new_phone": NEW_PHONE}, headers=auth(token)
    )

    # Somebody else registers that number before the code is entered.
    async with session_factory() as s:
        s.add(
            User(
                phone_hash=security.hash_phone(NEW_PHONE),
                phone_last4=security.phone_last4(NEW_PHONE),
            )
        )
        await s.commit()

    r = await client.post(
        "/users/me/phone/confirm",
        json={"new_phone": NEW_PHONE, "code": code_sent_to(sent, NEW_PHONE)},
        headers=auth(token),
    )
    assert r.status_code == 409, r.text


async def test_moving_to_your_own_number_is_rejected(client, users):
    token = await login(client, ADMIN_PHONE)
    r = await client.post(
        "/users/me/phone/start", json={"new_phone": ADMIN_PHONE}, headers=auth(token)
    )
    assert r.status_code == 409
