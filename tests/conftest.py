"""Test fixtures — an isolated in-memory database and an ASGI client per test.

The app under test is wired to a fresh SQLite database for every test, so
route tests can mutate freely without ordering dependencies.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import security
from app.db.models import Base, User
from app.db.session import get_session
from app.main import app
from app.services import otp_store

ADMIN_PHONE = "08000000001"
TENANT_PHONE = "08012345678"
OTHER_PHONE = "08098765432"


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker, None]:
    # A named in-memory database shared across connections for this test only.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=None,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory) -> AsyncGenerator[AsyncClient, None]:
    async def _override() -> AsyncGenerator:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_otp_store():
    """OTP state is process-global; reset it so quotas don't leak between tests."""
    otp_store._store = otp_store._MemoryStore()
    yield
    otp_store._store = otp_store._MemoryStore()


@pytest_asyncio.fixture
async def users(session_factory) -> dict[str, User]:
    """An admin and two tenants, keyed by role."""
    async with session_factory() as s:
        admin = User(
            phone_hash=security.hash_phone(ADMIN_PHONE),
            phone_last4="0001",
            display_name="RentSafe Admin",
            role="admin",
        )
        tenant = User(
            phone_hash=security.hash_phone(TENANT_PHONE),
            phone_last4="5678",
            display_name="Tola A.",
        )
        other = User(
            phone_hash=security.hash_phone(OTHER_PHONE),
            phone_last4="5432",
            display_name="Chidinma A.",
        )
        s.add_all([admin, tenant, other])
        await s.commit()
        for u in (admin, tenant, other):
            await s.refresh(u)
        return {"admin": admin, "tenant": tenant, "other": other}


async def login(client: AsyncClient, phone: str) -> str:
    """Run the real OTP flow and return an access token."""
    r = await client.post("/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = r.json()["dev_code"]
    r = await client.post("/auth/otp/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
