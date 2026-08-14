"""Claiming an agent profile.

`agents.profile_claimed` existed from the start and nothing could ever set it,
so the right-of-reply feature — which authorises on a matching `phone_hash` —
was unreachable by the agents it exists for.

The security property that matters: approving a claim hands someone the ability
to answer, on the public record, every tenant who has reviewed that agent. The
obvious abuse is a rival claiming a competitor's profile, or a landlord claiming
the agent who let their property in order to answer criticism of themselves. So
a claim is a *request*, and only an admin can grant it.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Agent, AgentClaim
from tests.conftest import ADMIN_PHONE, OTHER_PHONE, TENANT_PHONE, auth, login

SLUG = "chidi-okonkwo"


async def _agent(session_factory, **over) -> int:
    async with session_factory() as s:
        agent = Agent(
            name="Chidi Okonkwo",
            name_normalised="chidi okonkwo",
            slug=SLUG,
            company_name="Chidi Okonkwo Properties",
            total_reviews=41,
            **over,
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent.id


async def test_claiming_requires_a_session(client, users, session_factory):
    await _agent(session_factory)
    r = await client.post(f"/agents/{SLUG}/claim", json={"lasrera_number": "LAS123"})
    assert r.status_code in (401, 403)


async def test_a_claim_is_a_request_not_a_grant(client, users, session_factory):
    """Submitting must not confer any power by itself."""
    agent_id = await _agent(session_factory)
    token = await login(client, TENANT_PHONE)

    r = await client.post(
        f"/agents/{SLUG}/claim",
        json={"lasrera_number": "LAS123", "contact_email": "chidi@example.com"},
        headers=auth(token),
    )
    assert r.status_code == 202
    assert r.json()["status"] == "pending"

    async with session_factory() as s:
        agent = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    assert agent.profile_claimed is False
    assert agent.phone_hash is None, "claiming alone granted reply rights"


async def test_only_an_admin_can_decide_a_claim(client, users, session_factory):
    await _agent(session_factory)
    token = await login(client, TENANT_PHONE)
    await client.post(f"/agents/{SLUG}/claim", json={}, headers=auth(token))

    claim_id = 1
    r = await client.patch(
        f"/admin/moderation/claims/{claim_id}",
        json={"action": "approve"},
        headers=auth(token),
    )
    assert r.status_code == 403

    queue = await client.get("/admin/moderation/claims", headers=auth(token))
    assert queue.status_code == 403


async def test_approval_binds_the_claimant_and_unlocks_reply(
    client, users, session_factory
):
    agent_id = await _agent(session_factory)
    claimant = await login(client, TENANT_PHONE)
    await client.post(
        f"/agents/{SLUG}/claim", json={"lasrera_number": "LAS999"}, headers=auth(claimant)
    )

    admin = await login(client, ADMIN_PHONE)
    queue = await client.get("/admin/moderation/claims", headers=auth(admin))
    assert queue.status_code == 200
    item = queue.json()[0]
    assert item["agent_name"] == "Chidi Okonkwo"
    assert item["lasrera_number"] == "LAS999"

    decide = await client.patch(
        f"/admin/moderation/claims/{item['claim_id']}",
        json={"action": "approve"},
        headers=auth(admin),
    )
    assert decide.status_code == 200

    async with session_factory() as s:
        agent = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    assert agent.profile_claimed is True
    # This is what responses.py authorises the right of reply against.
    assert agent.phone_hash == users["tenant"].phone_hash


async def test_approval_does_not_grant_the_lasrera_badge(
    client, users, session_factory
):
    """Accepting that someone represents an agency is not the same act as
    checking their number against the LASRERA register."""
    agent_id = await _agent(session_factory)
    claimant = await login(client, TENANT_PHONE)
    await client.post(
        f"/agents/{SLUG}/claim", json={"lasrera_number": "LAS999"}, headers=auth(claimant)
    )

    admin = await login(client, ADMIN_PHONE)
    queue = await client.get("/admin/moderation/claims", headers=auth(admin))
    await client.patch(
        f"/admin/moderation/claims/{queue.json()[0]['claim_id']}",
        json={"action": "approve"},
        headers=auth(admin),
    )

    async with session_factory() as s:
        agent = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    assert agent.lasrera_verified is False


async def test_a_second_person_cannot_claim_a_claimed_profile(
    client, users, session_factory
):
    await _agent(session_factory, profile_claimed=True)
    token = await login(client, OTHER_PHONE)
    r = await client.post(f"/agents/{SLUG}/claim", json={}, headers=auth(token))
    assert r.status_code == 409


async def test_resubmitting_updates_rather_than_queueing_twice(
    client, users, session_factory
):
    """A claimant who mistyped their LASRERA number should just try again."""
    await _agent(session_factory)
    token = await login(client, TENANT_PHONE)

    await client.post(
        f"/agents/{SLUG}/claim", json={"lasrera_number": "WRONG"}, headers=auth(token)
    )
    await client.post(
        f"/agents/{SLUG}/claim", json={"lasrera_number": "LAS-CORRECT"}, headers=auth(token)
    )

    async with session_factory() as s:
        claims = (await s.execute(select(AgentClaim))).scalars().all()
    assert len(claims) == 1
    assert claims[0].lasrera_number == "LAS-CORRECT"


async def test_a_rejected_claim_cannot_simply_be_resubmitted(
    client, users, session_factory
):
    """Otherwise "no" is only ever a suggestion."""
    await _agent(session_factory)
    token = await login(client, TENANT_PHONE)
    await client.post(f"/agents/{SLUG}/claim", json={}, headers=auth(token))

    admin = await login(client, ADMIN_PHONE)
    queue = await client.get("/admin/moderation/claims", headers=auth(admin))
    await client.patch(
        f"/admin/moderation/claims/{queue.json()[0]['claim_id']}",
        json={"action": "reject", "note": "Could not verify."},
        headers=auth(admin),
    )

    again = await client.post(f"/agents/{SLUG}/claim", json={}, headers=auth(token))
    assert again.status_code == 409


async def test_a_decided_claim_cannot_be_decided_twice(client, users, session_factory):
    await _agent(session_factory)
    token = await login(client, TENANT_PHONE)
    await client.post(f"/agents/{SLUG}/claim", json={}, headers=auth(token))

    admin = await login(client, ADMIN_PHONE)
    queue = await client.get("/admin/moderation/claims", headers=auth(admin))
    claim_id = queue.json()[0]["claim_id"]

    first = await client.patch(
        f"/admin/moderation/claims/{claim_id}",
        json={"action": "approve"},
        headers=auth(admin),
    )
    assert first.status_code == 200

    second = await client.patch(
        f"/admin/moderation/claims/{claim_id}",
        json={"action": "reject"},
        headers=auth(admin),
    )
    assert second.status_code == 409


async def test_claiming_an_unknown_agent_is_404(client, users):
    token = await login(client, TENANT_PHONE)
    r = await client.post("/agents/nobody-at-all/claim", json={}, headers=auth(token))
    assert r.status_code == 404
