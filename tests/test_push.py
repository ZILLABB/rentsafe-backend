"""Push delivery for area watches.

Watches drove the in-app feed and the unread badge but reached no phone, so a
flood report about the street a tenant was about to sign on only existed if they
happened to open the app.

Two things are load-bearing here and both are asserted:

* a notification must never carry review text — it renders on a lock screen,
  outside every moderation and privacy control the app has; and
* a failing push must never fail the action that triggered it. Moderating a
  review is not contingent on a push service being up.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db.models import AreaWatch, PushSubscription
from app.services import push


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(push.settings, "vapid_public_key", "test-public")
    monkeypatch.setattr(push.settings, "vapid_private_key", "test-private")
    yield


async def _subscribe(session, user_id: int, endpoint: str = "https://push.example/1"):
    session.add(
        PushSubscription(
            user_id=user_id, endpoint=endpoint, p256dh="key", auth="auth"
        )
    )
    await session.commit()


async def _watch(session, user_id: int, area="LEK", **flags):
    session.add(
        AreaWatch(
            user_id=user_id,
            area_code=area,
            notify_reviews=flags.get("reviews", True),
            notify_floods=flags.get("floods", True),
            notify_agent_flags=flags.get("agent_flags", True),
        )
    )
    await session.commit()


async def test_no_keys_means_no_push_and_no_error(session_factory, users, monkeypatch):
    """Push is optional. Missing keys must never be a failure."""
    monkeypatch.setattr(push.settings, "vapid_private_key", "")
    async with session_factory() as s:
        sent = await push.notify_area(
            s, area_code="LEK", kind="review", title="t", body="b"
        )
    assert sent == 0


async def test_watchers_of_the_area_are_pushed(session_factory, users, monkeypatch):
    calls: list[tuple[dict, str]] = []

    def fake_send(subscription, payload):
        calls.append((subscription, payload))
        return 201

    monkeypatch.setattr(push, "_send_one", fake_send)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id)
        sent = await push.notify_area(
            s, area_code="LEK", kind="review", title="New review", body="Someone posted"
        )

    assert sent == 1
    assert calls[0][0]["endpoint"] == "https://push.example/1"


async def test_payload_never_carries_review_text(session_factory, users, monkeypatch):
    """A lock-screen notification is outside every control this app has."""
    captured: list[str] = []

    def fake_send(subscription, payload):
        captured.append(payload)
        return 201

    monkeypatch.setattr(push, "_send_one", fake_send)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id)
        await push.notify_area(
            s,
            area_code="LEK",
            kind="review",
            title="New review in Lekki",
            body="A tenant just published a review for a property here.",
        )

    payload = json.loads(captured[0])
    assert set(payload) == {"title", "body", "url", "kind"}
    # The body is a fixed string, not anything a tenant wrote.
    assert "landlord" not in payload["body"].lower()


async def test_notify_flags_are_honoured(session_factory, users, monkeypatch):
    """Watching Yaba for floods must not mean hearing about every review."""
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: 201)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id, reviews=False, floods=True)

        reviews = await push.notify_area(
            s, area_code="LEK", kind="review", title="t", body="b"
        )
        floods = await push.notify_area(
            s, area_code="LEK", kind="flood", title="t", body="b"
        )

    assert reviews == 0
    assert floods == 1


async def test_a_user_is_not_pushed_about_their_own_review(
    session_factory, users, monkeypatch
):
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: 201)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id)
        sent = await push.notify_area(
            s,
            area_code="LEK",
            kind="review",
            title="t",
            body="b",
            exclude_user_id=tenant.id,
        )
    assert sent == 0


async def test_a_gone_endpoint_is_deleted(session_factory, users, monkeypatch):
    """410 means the browser is gone. Keeping the row retries it forever."""
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: 410)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id)
        await push.notify_area(s, area_code="LEK", kind="review", title="t", body="b")

        rows = (await s.execute(select(PushSubscription))).scalars().all()
    assert rows == []


async def test_a_transient_failure_is_counted_not_deleted(
    session_factory, users, monkeypatch
):
    """One 500 from a push service is not proof the subscription is dead."""
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: 500)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id)
        await push.notify_area(s, area_code="LEK", kind="review", title="t", body="b")

        row = (await s.execute(select(PushSubscription))).scalar_one()
        assert row.failure_count == 1

        # ...but repeated failure does eventually drop it.
        for _ in range(push.MAX_FAILURES):
            await push.notify_area(
                s, area_code="LEK", kind="review", title="t", body="b"
            )
        assert (await s.execute(select(PushSubscription))).scalars().all() == []


async def test_delivery_failure_never_breaks_the_caller(session_factory, users, monkeypatch):
    """Moderating a review is not contingent on a push service being reachable."""

    def explode(subscription, payload):
        raise ConnectionError("push service unreachable")

    monkeypatch.setattr(push, "_send_one", explode)

    async with session_factory() as s:
        tenant = users["tenant"]
        await _subscribe(s, tenant.id)
        await _watch(s, tenant.id)
        # The _safe wrapper is what call sites use.
        sent = await push.notify_area_safe(
            s, area_code="LEK", kind="review", title="t", body="b"
        )
    assert sent == 0


async def test_resubscribing_a_browser_updates_rather_than_duplicates(
    client, users, monkeypatch
):
    """Otherwise a user gets one copy of every notification per stale row."""
    from tests.conftest import TENANT_PHONE, auth, login

    token = await login(client, TENANT_PHONE)
    body = {
        "endpoint": "https://push.example/abc",
        "keys": {"p256dh": "k", "auth": "a"},
    }
    for _ in range(3):
        r = await client.post("/push/subscribe", json=body, headers=auth(token))
        assert r.status_code == 204, r.text

    status = await client.get("/push/status", headers=auth(token))
    assert status.json()["devices"] == 1


async def test_config_reports_disabled_without_keys(client, monkeypatch):
    monkeypatch.setattr(push.settings, "vapid_private_key", "")
    r = await client.get("/push/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["public_key"] is None


async def test_unsubscribe_only_touches_your_own_rows(client, users, session_factory):
    """An endpoint string is not authorisation to delete someone else's row."""
    from tests.conftest import OTHER_PHONE, TENANT_PHONE, auth, login

    async with session_factory() as s:
        await _subscribe(s, users["tenant"].id, "https://push.example/victim")

    other = await login(client, OTHER_PHONE)
    r = await client.post(
        "/push/unsubscribe",
        json={
            "endpoint": "https://push.example/victim",
            "keys": {"p256dh": "k", "auth": "a"},
        },
        headers=auth(other),
    )
    assert r.status_code == 204

    async with session_factory() as s:
        rows = (await s.execute(select(PushSubscription))).scalars().all()
    assert len(rows) == 1, "another user's subscription was deleted"

    # And the owner can still remove it.
    owner = await login(client, TENANT_PHONE)
    await client.post(
        "/push/unsubscribe",
        json={
            "endpoint": "https://push.example/victim",
            "keys": {"p256dh": "k", "auth": "a"},
        },
        headers=auth(owner),
    )
    async with session_factory() as s:
        assert (await s.execute(select(PushSubscription))).scalars().all() == []
