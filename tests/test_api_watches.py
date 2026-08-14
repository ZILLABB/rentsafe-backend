"""Area watches and the unread badge.

The badge is the part worth testing hardest: it was previously hardcoded on,
so it signalled nothing. A badge that lies is worse than no badge.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.core import geohash
from app.db.models import LGA, Neighbourhood, Property, Review, User
from tests.conftest import OTHER_PHONE, TENANT_PHONE, auth, login

LEK = "ETI-LEK-7F3A2B-0041"
YAB = "LAM-YAB-Q8MN4C-0007"


@pytest.fixture
async def areas(session_factory):
    """Two areas, one property each, one approved review each."""
    async with session_factory() as s:
        s.add(LGA(code="ETI", name="Eti-Osa"))
        s.add(LGA(code="LAM", name="Lagos Mainland"))
        s.add(Neighbourhood(code="LEK", name="Lekki Phase 1", lga_code="ETI"))
        s.add(Neighbourhood(code="YAB", name="Yaba", lga_code="LAM"))

        user = (
            await s.execute(select(User).where(User.role == "tenant").limit(1))
        ).scalar_one_or_none()
        if user is None:
            user = User(phone_hash="seedhash", phone_last4="0000")
            s.add(user)
            await s.flush()

        for pid, lat, lng, lga, area in [
            (LEK, 6.4474, 3.4736, "ETI", "LEK"),
            (YAB, 6.5095, 3.3711, "LAM", "YAB"),
        ]:
            gh8, gh7 = geohash.encode_pair(lat, lng)
            prop = Property(
                property_id=pid, geohash_7=gh7, geohash_8=gh8,
                lat=lat, lng=lng, address_local=f"A place in {area}",
                lga_code=lga, neighbourhood_code=area,
            )
            s.add(prop)
            await s.flush()
            s.add(
                Review(
                    property_id=prop.id,
                    user_id=user.id,
                    tenancy_start=dt.date(2024, 1, 1),
                    rating_landlord=4, rating_agent=4, rating_property=4,
                    rating_water=4, rating_power=4, rating_security=4,
                    rating_noise=4, rating_flooding=4, rating_neighbourhood=4,
                    rating_value=4,
                    text_positives="Quiet street with steady water.",
                    moderation_status="approved",
                    created_at=dt.datetime.now(dt.UTC),
                )
            )
        await s.commit()


async def test_watching_requires_sign_in(client, areas):
    assert (await client.put("/areas/LEK/watch")).status_code == 401


async def test_unknown_area_cannot_be_watched(client, users, areas):
    token = await login(client, TENANT_PHONE)
    r = await client.put("/areas/NOPE/watch", headers=auth(token))
    assert r.status_code == 404


async def test_watch_and_unwatch(client, users, areas):
    token = await login(client, TENANT_PHONE)
    assert (await client.get("/users/me/watches", headers=auth(token))).json() == []

    assert (await client.put("/areas/LEK/watch", headers=auth(token))).status_code == 204

    watches = (await client.get("/users/me/watches", headers=auth(token))).json()
    assert [w["area_code"] for w in watches] == ["LEK"]
    assert watches[0]["area_name"] == "Lekki Phase 1"

    assert (
        await client.delete("/areas/LEK/watch", headers=auth(token))
    ).status_code == 204
    assert (await client.get("/users/me/watches", headers=auth(token))).json() == []


async def test_watching_twice_is_idempotent(client, users, areas):
    token = await login(client, TENANT_PHONE)
    for _ in range(3):
        assert (
            await client.put("/areas/LEK/watch", headers=auth(token))
        ).status_code == 204
    assert len((await client.get("/users/me/watches", headers=auth(token))).json()) == 1


async def test_watches_are_private_to_each_user(client, users, areas):
    mine = await login(client, TENANT_PHONE)
    theirs = await login(client, OTHER_PHONE)
    await client.put("/areas/LEK/watch", headers=auth(mine))

    assert len((await client.get("/users/me/watches", headers=auth(mine))).json()) == 1
    assert (await client.get("/users/me/watches", headers=auth(theirs))).json() == []


async def test_feed_narrows_to_watched_areas(client, users, areas):
    """The whole point: a Yaba hunter shouldn't get Lekki's activity."""
    token = await login(client, TENANT_PHONE)

    everything = (await client.get("/alerts", headers=auth(token))).json()
    assert {a["area_code"] for a in everything if a["area_code"]} == {"LEK", "YAB"}

    await client.put("/areas/YAB/watch", headers=auth(token))

    watched = (await client.get("/alerts", headers=auth(token))).json()
    codes = {a["area_code"] for a in watched if a["area_code"]}
    assert codes == {"YAB"}, codes


async def test_scope_all_overrides_watches(client, users, areas):
    token = await login(client, TENANT_PHONE)
    await client.put("/areas/YAB/watch", headers=auth(token))

    everything = (await client.get("/alerts?scope=all", headers=auth(token))).json()
    assert {a["area_code"] for a in everything if a["area_code"]} == {"LEK", "YAB"}


async def test_explicit_watched_scope_with_no_watches_returns_nothing(
    client, users, areas
):
    """Falling back to all-Lagos here would look like watches were broken."""
    token = await login(client, TENANT_PHONE)
    r = await client.get("/alerts?scope=watched", headers=auth(token))
    assert r.json() == []


async def test_anonymous_visitors_still_see_all_lagos(client, users, areas):
    feed = (await client.get("/alerts")).json()
    assert {a["area_code"] for a in feed if a["area_code"]} == {"LEK", "YAB"}


# ------------------------------------------------------------- unread badge

async def test_unread_is_zero_without_watches(client, users, areas):
    token = await login(client, TENANT_PHONE)
    body = (await client.get("/alerts/unread", headers=auth(token))).json()
    assert body == {"unread": 0, "watching": 0}


async def test_unread_is_zero_for_anonymous(client, areas):
    assert (await client.get("/alerts/unread")).json() == {"unread": 0, "watching": 0}


async def test_unread_counts_only_watched_areas(client, users, areas):
    token = await login(client, TENANT_PHONE)
    await client.put("/areas/YAB/watch", headers=auth(token))

    body = (await client.get("/alerts/unread", headers=auth(token))).json()
    assert body["watching"] == 1
    # One approved review in Yaba, and the user has never opened their alerts.
    assert body["unread"] == 1


async def test_marking_read_clears_the_badge(client, users, areas):
    token = await login(client, TENANT_PHONE)
    await client.put("/areas/YAB/watch", headers=auth(token))
    assert (await client.get("/alerts/unread", headers=auth(token))).json()["unread"] == 1

    assert (await client.post("/alerts/read", headers=auth(token))).status_code == 204
    assert (await client.get("/alerts/unread", headers=auth(token))).json()["unread"] == 0


async def test_new_activity_after_reading_marks_unread_again(
    client, users, areas, session_factory
):
    token = await login(client, TENANT_PHONE)
    await client.put("/areas/YAB/watch", headers=auth(token))
    await client.post("/alerts/read", headers=auth(token))
    assert (await client.get("/alerts/unread", headers=auth(token))).json()["unread"] == 0

    async with session_factory() as s:
        prop = (
            await s.execute(select(Property).where(Property.property_id == YAB))
        ).scalar_one()
        user = (await s.execute(select(User))).scalars().first()
        s.add(
            Review(
                property_id=prop.id,
                user_id=user.id,
                tenancy_start=dt.date(2024, 6, 1),
                rating_landlord=3, rating_agent=3, rating_property=3,
                rating_water=3, rating_power=3, rating_security=3,
                rating_noise=3, rating_flooding=3, rating_neighbourhood=3,
                rating_value=3,
                text_positives="Later review.",
                moderation_status="approved",
                created_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
            )
        )
        await s.commit()

    assert (await client.get("/alerts/unread", headers=auth(token))).json()["unread"] == 1


async def test_unapproved_reviews_never_count_toward_unread(
    client, users, areas, session_factory
):
    """The badge must not leak the existence of held content."""
    token = await login(client, TENANT_PHONE)
    await client.put("/areas/YAB/watch", headers=auth(token))
    await client.post("/alerts/read", headers=auth(token))

    async with session_factory() as s:
        prop = (
            await s.execute(select(Property).where(Property.property_id == YAB))
        ).scalar_one()
        user = (await s.execute(select(User))).scalars().first()
        s.add(
            Review(
                property_id=prop.id,
                user_id=user.id,
                tenancy_start=dt.date(2024, 6, 1),
                rating_landlord=1, rating_agent=1, rating_property=1,
                rating_water=1, rating_power=1, rating_security=1,
                rating_noise=1, rating_flooding=1, rating_neighbourhood=1,
                rating_value=1,
                text_warnings="Held for moderation.",
                moderation_status="flagged",
                created_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
            )
        )
        await s.commit()

    assert (await client.get("/alerts/unread", headers=auth(token))).json()["unread"] == 0
