"""Guards against blocking the event loop.

Every other test in this suite passes whether or not the app can serve two
users at once, because they exercise one request at a time. These fail if a
handler goes back to doing blocking work on the loop.

Both defects these cover were real and shipped: address search held the loop
for the full Nominatim round trip plus a 1.1s courtesy sleep, and image
processing held it for ~285ms of CPU per upload.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from app.api.v1 import photos, places
from app.services import media, opendata
from tests.conftest import TENANT_PHONE, auth, login
from tests.test_api_photos import (  # noqa: F401 — fixtures used by name
    PROPERTY_ID,
    make_image,
    media_root,
    property_row,
)

# How long the fake upstream takes. Long enough that blocking is unmistakable,
# short enough to keep the suite quick.
UPSTREAM_DELAY = 0.4


class Heartbeat:
    """Counts how many times the event loop got a turn.

    Measuring one unrelated request's latency isn't enough: a synchronous call
    blocks the loop *before* that request starts, so the request itself still
    looks fast and the block goes unnoticed. A ticker running across the whole
    window is the honest instrument — if the loop stalls, the ticks stop.
    """

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.ticks = 0
        self._running = False
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(self.interval)
            self.ticks += 1

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        # Yield once so the ticker is actually running before the work starts —
        # otherwise a task created and immediately followed by blocking code
        # never gets scheduled, and the test reads zero ticks whether or not
        # the code under test is at fault.
        await asyncio.sleep(0)

    async def stop(self) -> int:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return self.ticks

    @property
    def expected(self) -> int:
        """Ticks a healthy loop should manage over UPSTREAM_DELAY."""
        return int(UPSTREAM_DELAY / self.interval * 0.5)


async def test_address_search_does_not_block_other_requests(
    client, monkeypatch, tmp_path
):
    """A slow geocode must not stall everybody else's page loads.

    The delay is injected at the HTTP layer rather than at ``geocode_async``,
    so the test measures whichever client the handler actually reaches for.
    Patching the async helper by name would let a switch back to the blocking
    version slip through unnoticed.
    """
    monkeypatch.setattr(opendata, "CACHE_DIR", tmp_path)  # force a cache miss

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list:
            return []

    import httpx

    # The test client is itself an httpx.AsyncClient, so the fake has to leave
    # everything except the Nominatim call alone — otherwise it intercepts the
    # very health check we're using to measure responsiveness.
    real_async_get = httpx.AsyncClient.get

    async def slow_async_get(self, url, *a, **kw):
        if opendata.NOMINATIM_URL in str(url):
            await asyncio.sleep(UPSTREAM_DELAY)
            return FakeResponse()
        return await real_async_get(self, url, *a, **kw)

    def slow_sync_get(self, url, *a, **kw):
        time.sleep(UPSTREAM_DELAY)  # what the blocking client would do
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", slow_async_get)
    monkeypatch.setattr(httpx.Client, "get", slow_sync_get)

    beat = Heartbeat()
    await beat.start()
    await client.get("/places/search?q=Some Street Nobody Cached")
    ticks = await beat.stop()

    assert ticks >= beat.expected, (
        f"the loop only got {ticks} turns (expected >= {beat.expected}) while "
        f"a geocode was in flight — it is blocked for the duration"
    )


async def test_image_processing_does_not_block_other_requests(
    client, users, property_row, media_root, monkeypatch
):
    """CPU-bound upload handling belongs off the loop."""
    token = await login(client, TENANT_PHONE)

    # Built before the measurement window: fixture prep is not what we're
    # timing, and drawing the image is itself synchronous work.
    payload = make_image(with_gps=False)

    real = media.process_upload
    called = False

    def slow_cpu(raw, **kw):
        # Stands in for Pillow decode + resize + hash, which is what actually
        # costs the time. A plain sleep is the right shape: it holds the thread
        # without holding the GIL, exactly like Pillow does.
        nonlocal called
        called = True
        time.sleep(UPSTREAM_DELAY)
        return real(raw, **kw)

    monkeypatch.setattr(media, "process_upload", slow_cpu)

    beat = Heartbeat()
    await beat.start()
    response = await client.post(
        f"/properties/{PROPERTY_ID}/photos",
        files={"file": ("a.jpg", payload, "image/jpeg")},
        headers=auth(token),
    )
    ticks = await beat.stop()

    # Check the request actually did the work first. Without this, a handler
    # that 404s early looks identical to a blocked loop — few ticks either way —
    # and the test fails for the wrong reason.
    assert response.status_code == 201, response.text
    assert called, "process_upload never ran; the tick count means nothing"

    assert ticks >= beat.expected, (
        f"the loop only got {ticks} turns (expected >= {beat.expected}) while "
        f"an image was processed — the work is still on the event loop"
    )


def test_request_handlers_do_not_call_the_blocking_geocoder():
    """A static backstop, in case someone reaches for the obvious name.

    `opendata.geocode` is the synchronous version kept for the importer CLI.
    Calling it from a handler reintroduces the original defect, and the timing
    test above only catches it if the cache happens to miss.
    """
    source = inspect.getsource(places)
    # Either awaitable entry point is fine; geocode_progressive awaits
    # geocode_async internally.
    assert "opendata.geocode_async" in source or "opendata.geocode_progressive" in source
    assert "opendata.geocode(" not in source, (
        "places.py calls the blocking geocoder; use geocode_async in handlers"
    )


def test_upload_handler_keeps_image_work_off_the_loop():
    source = inspect.getsource(photos)
    assert "run_in_threadpool" in source, (
        "upload_photo must not call media.process_upload directly — it is "
        "~285ms of CPU on the event loop"
    )


@pytest.mark.parametrize("fn_name", ["geocode_async"])
def test_async_helpers_are_actually_coroutines(fn_name):
    fn = getattr(opendata, fn_name)
    assert inspect.iscoroutinefunction(fn)


def test_sync_geocoder_still_exists_for_the_importer():
    """The CLI runs offline against the cache and has no loop to protect."""
    assert not inspect.iscoroutinefunction(opendata.geocode)


async def test_rate_limiter_waits_without_blocking_the_loop():
    """The limiter spaces calls out on the loop, not with time.sleep."""
    limiter = opendata.AsyncRateLimiter(0.2)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    start = time.perf_counter()
    await limiter.acquire()
    await limiter.acquire()  # must wait ~0.2s for its slot
    elapsed = time.perf_counter() - start
    await beat

    assert elapsed >= 0.18, "limiter let two calls through without spacing"
    # If acquire() had slept synchronously the heartbeat would have been frozen.
    assert ticks >= 10, f"only {ticks} heartbeats — the loop was blocked"


async def test_shared_slot_limits_the_fleet_not_just_the_process():
    """Nominatim's cap is per application, so the budget must be shared.

    Four workers each politely spacing their own calls still make four requests
    a second. This asserts the shared claim rejects the extras.
    """
    from app.services import opendata, otp_store

    otp_store._store = otp_store._MemoryStore()
    limiter = opendata.AsyncRateLimiter(1.1, shared_key="test-upstream")

    # Simulate separate processes racing for the same second: each has its own
    # in-process interval (fresh limiter) but shares the store.
    workers = [opendata.AsyncRateLimiter(1.1, shared_key="test-upstream") for _ in range(4)]
    granted = [w._claim_shared_slot() for w in workers]

    assert granted.count(True) == 1, (
        f"{granted.count(True)} of 4 workers were allowed through in one second"
    )
    assert limiter.shared_key == "test-upstream"

    # A later second is a different slot and opens again. Awaited, not slept
    # through — a blocking sleep in this file would be the very defect the rest
    # of it guards against.
    await asyncio.sleep(1.2)
    assert workers[0]._claim_shared_slot() is True


async def test_shared_limiter_survives_a_dead_store(monkeypatch):
    """A limiter that breaks the request when Redis blips is worse than no limiter."""
    from app.services import opendata, otp_store

    class Broken:
        def incr(self, *a, **kw):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(otp_store, "_store", Broken())
    limiter = opendata.AsyncRateLimiter(1.1, shared_key="test-upstream")
    assert limiter._claim_shared_slot() is True


async def test_object_store_writes_stay_off_the_event_loop():
    """boto3 is synchronous and has no async client.

    On local disk `store.save` is a fast write, which is why a blocking call
    survived review. Once media moves to object storage the same line becomes a
    network round trip, and on the loop it stalls every other request for its
    duration.
    """
    source = inspect.getsource(photos)
    assert "run_in_threadpool(media.store.save" in source, (
        "media.store.save must be awaited off the loop — with S3 configured it "
        "is a blocking network call"
    )


async def test_uploads_cannot_exhaust_the_thread_pool():
    """A burst of uploads must not starve every other blocking call."""
    assert photos.UPLOAD_CONCURRENCY < 40, (
        "upload concurrency must stay below Starlette's default thread pool "
        "size, or image decoding takes every thread"
    )
    assert photos._upload_slots._value == photos.UPLOAD_CONCURRENCY
