"""Live drive-time estimates from a routing provider.

Commute times on RentSafe are tenant reports: what people actually experienced,
which is the whole point — a routing API models traffic, but it does not model
a broken-down trailer on Third Mainland Bridge every Monday.

A routing estimate is still worth having *beside* those reports, for one
specific reason: it is the number the tenant would have got from their phone,
so the gap between it and reality is itself the finding. "Maps says 35 minutes,
tenants say 95" tells a renter more than either figure alone.

Two rules this module exists to enforce:

* **Never invent one.** With no key configured, the estimate stays ``None`` and
  the UI says the comparison is unavailable. A plausible-looking guess here
  would corrupt exactly the comparison that makes the feature worth having.
* **Never let it outrank a tenant.** The caller keeps reported figures as the
  headline; this is the annotation.

Both Google Routes and Mapbox Directions are supported because whichever key a
deployment already has is the one it should use. Estimates are cached: they cost
money per call, and traffic does not change meaningfully within the hour.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MAPBOX_DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic"

# Traffic-aware estimates go stale, but not within an hour, and every call is
# billable. Keyed on coordinates rounded to ~100m so nearby properties share.
CACHE_TTL_S = 3600
_COORD_PRECISION = 3

TIMEOUT_S = 8.0


class RoutingUnavailable(Exception):
    """The provider could not be reached or is not configured."""


def _cache_key(origin: tuple[float, float], dest: tuple[float, float]) -> str:
    o = f"{origin[0]:.{_COORD_PRECISION}f},{origin[1]:.{_COORD_PRECISION}f}"
    d = f"{dest[0]:.{_COORD_PRECISION}f},{dest[1]:.{_COORD_PRECISION}f}"
    return f"route:{o}:{d}"


def is_configured() -> bool:
    """Whether any routing provider has credentials.

    Exposed so the API can tell the UI *why* an estimate is missing — "not
    configured" and "no route found" are different things to a user.
    """
    return bool(settings.google_maps_api_key or settings.mapbox_access_token)


async def _google(
    origin: tuple[float, float], dest: tuple[float, float]
) -> int | None:
    body = {
        "origin": {
            "location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}
        },
        "destination": {
            "location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}
        },
        "travelMode": "DRIVE",
        # The whole reason to ask a routing API at all: the traffic-aware
        # number is the one a tenant's phone would have shown them.
        "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {
        "X-Goog-Api-Key": settings.google_maps_api_key,
        # Google bills by the fields requested, so ask for exactly one.
        "X-Goog-FieldMask": "routes.duration",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(GOOGLE_ROUTES_URL, json=body, headers=headers)
        r.raise_for_status()
        routes = r.json().get("routes") or []
    if not routes:
        return None
    # Duration comes back as a protobuf duration string, e.g. "2280s".
    raw = str(routes[0].get("duration", "")).rstrip("s")
    return round(float(raw) / 60) if raw else None


async def _mapbox(
    origin: tuple[float, float], dest: tuple[float, float]
) -> int | None:
    # Mapbox takes lng,lat — the opposite order to everything else here, which
    # is a classic source of routes that silently land in the Gulf of Guinea.
    path = f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    params = {
        "access_token": settings.mapbox_access_token,
        "overview": "false",
        "alternatives": "false",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.get(f"{MAPBOX_DIRECTIONS_URL}/{path}", params=params)
        r.raise_for_status()
        routes = r.json().get("routes") or []
    if not routes:
        return None
    return round(float(routes[0]["duration"]) / 60)


async def drive_estimate_min(
    origin: tuple[float, float], dest: tuple[float, float]
) -> int | None:
    """Traffic-aware drive time in minutes, or ``None`` if we cannot know it.

    ``None`` is a legitimate, common answer: no key configured, provider down,
    or no drivable route. Every one of those must read as "we don't know", never
    as a number.
    """
    if not is_configured():
        return None

    from app.services import otp_store

    key = _cache_key(origin, dest)
    try:
        cached = otp_store._store.get(key)
        if cached is not None:
            return int(cached) if cached != "none" else None
    except Exception:  # noqa: BLE001 - a cache miss must never fail the request
        logger.warning("Routing cache unavailable; calling the provider directly")

    try:
        if settings.google_maps_api_key:
            minutes = await _google(origin, dest)
        else:
            minutes = await _mapbox(origin, dest)
    except Exception as exc:  # noqa: BLE001 - network, quota, auth, bad response
        # Logged, not raised: a commute tab that 500s because a third party is
        # down is worse than one missing a single annotation.
        logger.warning("Routing lookup failed: %s", type(exc).__name__)
        return None

    try:
        # "none" is cached too, so a provider outage doesn't mean hammering it
        # on every page load.
        value = str(minutes) if minutes is not None else "none"
        otp_store._store.set(key, value, CACHE_TTL_S)
    except Exception:
        logger.debug("Could not cache routing result", exc_info=True)
    return minutes
