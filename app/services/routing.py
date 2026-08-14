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
# FOSSGIS runs a public Valhalla instance over OpenStreetMap. No key, no bill,
# and no live traffic — it answers "how long would this take on empty roads".
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"

# What kind of number came back. These are not interchangeable and the UI must
# not present them with the same words:
#   traffic    a provider's model of current conditions — what a phone shows
#   free_flow  the road network at its speed limits, i.e. 4am with nobody about
TRAFFIC = "traffic"
FREE_FLOW = "free_flow"

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


def has_traffic_provider() -> bool:
    """Whether a paid, traffic-aware provider is configured."""
    return bool(settings.google_maps_api_key or settings.mapbox_access_token)


def is_configured() -> bool:
    """Whether any estimate is obtainable at all.

    Always true now that the keyless fallback exists, but kept so the API can
    still distinguish "we have no source" from "we asked and got nothing".
    """
    return True


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


async def _valhalla(
    origin: tuple[float, float], dest: tuple[float, float]
) -> int | None:
    """Free-flow drive time from FOSSGIS's public Valhalla instance.

    Community-run and donation-funded, so the results are cached hard and this
    is never called in a loop. Attribution is owed to OpenStreetMap.
    """
    body = {
        "locations": [
            {"lat": origin[0], "lon": origin[1]},
            {"lat": dest[0], "lon": dest[1]},
        ],
        "costing": "auto",
        # No manoeuvre list needed; only the summary is used.
        "directions_options": {"units": "kilometers"},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(VALHALLA_URL, json=body)
        r.raise_for_status()
        payload = r.json()
    seconds = payload.get("trip", {}).get("summary", {}).get("time")
    return round(float(seconds) / 60) if seconds else None


async def drive_estimate_min(
    origin: tuple[float, float], dest: tuple[float, float]
) -> tuple[int | None, str | None]:
    """Drive time in minutes and *what kind of number it is*.

    Returns ``(minutes, kind)`` where kind is ``traffic`` or ``free_flow``.
    The pair is deliberately inseparable: 22 minutes free-flow and 22 minutes
    in traffic describe completely different journeys, and a caller that gets
    only the number cannot label it honestly.

    ``(None, None)`` is a legitimate, common answer — provider down, or no
    drivable route. It must read as "we don't know", never as a number.
    """
    from app.services import otp_store

    kind = TRAFFIC if has_traffic_provider() else FREE_FLOW
    key = f"{_cache_key(origin, dest)}:{kind}"

    try:
        cached = otp_store._store.get(key)
        if cached is not None:
            return (int(cached) if cached != "none" else None), kind
    except Exception:  # noqa: BLE001 - a cache miss must never fail the request
        logger.warning("Routing cache unavailable; calling the provider directly")

    try:
        if settings.google_maps_api_key:
            minutes = await _google(origin, dest)
        elif settings.mapbox_access_token:
            minutes = await _mapbox(origin, dest)
        else:
            minutes = await _valhalla(origin, dest)
    except Exception as exc:  # noqa: BLE001 - network, quota, auth, bad response
        # Logged, not raised: a commute tab that 500s because a third party is
        # down is worse than one missing a single annotation.
        logger.warning("Routing lookup failed: %s", type(exc).__name__)
        return None, kind

    try:
        # "none" is cached too, so a provider outage doesn't mean hammering a
        # donation-funded service on every page load.
        value = str(minutes) if minutes is not None else "none"
        otp_store._store.set(key, value, CACHE_TTL_S)
    except Exception:
        logger.debug("Could not cache routing result", exc_info=True)
    return minutes, kind
