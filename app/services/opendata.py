"""Clients for the open datasets RentSafe can legitimately build on.

What can and cannot be sourced externally is a product decision, not just a
technical one:

  * Reference geography — LGA and neighbourhood boundaries, transit stops,
    elevation — is public, open-licensed and objective. Import it.
  * Reviews, actual rents paid, and real commute times cannot be. Listing sites
    publish *asking* prices, which is the exact distortion this product exists
    to correct, and their terms forbid scraping. These stay first-party.

Every import records where a value came from (see ``DataSource``), because a
transparency product that can't say where its numbers came from has a problem.

Licences, which must be honoured downstream:
  * OpenStreetMap — ODbL 1.0. Attribution required; share-alike applies to
    derived geodata.
  * Open-Elevation (SRTM/NASADEM) — public domain.
  * Nominatim — ODbL, and its usage policy caps automated use (1 req/s, real
    User-Agent). We cache aggressively and never bulk-geocode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Nominatim's usage policy requires a genuine identifying User-Agent.
USER_AGENT = "RentSafe-Lagos/0.1 (rental transparency; contact dev@rentsafe.local)"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"


class OpenDataError(RuntimeError):
    pass


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def load_cached(key: str) -> Any | None:
    path = _cache_path(key)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_cached(key: str, payload: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _get_or_fetch(key: str, fetch, *, offline: bool) -> Any:
    """Cache-first. ``offline=True`` never touches the network.

    Imports are reproducible and CI-safe because of this: the committed cache
    is the input, and refreshing it is a deliberate act.
    """
    cached = load_cached(key)
    if cached is not None:
        return cached
    if offline:
        raise OpenDataError(
            f"No cached data for {key!r} and running offline. "
            f"Re-run the importer without --offline to fetch it."
        )
    payload = fetch()
    save_cached(key, payload)
    return payload


class AsyncRateLimiter:
    """Spaces out calls to a courtesy-limited upstream without blocking.

    Nominatim asks for at most one request per second. The obvious way to
    honour that is ``time.sleep`` — which, inside an ``async def`` handler,
    stops the entire event loop and every other request in the process along
    with it. This waits on the loop instead, so one user's address lookup no
    longer stalls everybody else's page load.

    Two budgets are enforced, because the process-local one is not enough:

    * an in-process interval, which keeps a single worker orderly, and
    * a **fleet-wide slot** claimed in the shared store, because the limit
      Nominatim publishes is per *application*, not per process. Four uvicorn
      workers each politely spacing their own calls still make four requests a
      second, which is how a free service other people depend on ends up
      banning our IP.

    The shared claim is a per-second counter: everyone racing for the same
    second increments the same key, and only the first ``per_interval`` winners
    proceed. INCR is atomic, so no lock is needed. When no shared store is
    configured (local dev on the memory store) this degrades to the in-process
    behaviour, which is correct for one process.
    """

    def __init__(self, min_interval_s: float, *, shared_key: str | None = None) -> None:
        self.min_interval = min_interval_s
        self.shared_key = shared_key
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    def _claim_shared_slot(self) -> bool:
        """Try to take this second's fleet-wide slot. False means try again."""
        if not self.shared_key:
            return True
        from app.services import otp_store

        slot = int(time.time() / self.min_interval)
        key = f"ratelimit:{self.shared_key}:{slot}"
        try:
            # TTL of two intervals: long enough that the counter outlives the
            # window it guards, short enough that keys don't accumulate.
            used = otp_store._store.incr(key, max(2, int(self.min_interval * 2) + 1))
        except Exception:  # noqa: BLE001 - a limiter must never break the request
            logger.warning("Shared rate-limit store unavailable; using local budget")
            return True
        return used <= 1

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed = time.monotonic() + self.min_interval

        # Then wait for a fleet-wide slot. Bounded so a busy fleet degrades into
        # a slow lookup rather than a request that never returns.
        deadline = time.monotonic() + SHARED_SLOT_MAX_WAIT_S
        while not self._claim_shared_slot():
            if time.monotonic() > deadline:
                logger.warning("Gave up waiting for a shared %s slot", self.shared_key)
                return
            await asyncio.sleep(self.min_interval / 4)


# How long a request will wait for its turn at a shared upstream before going
# ahead anyway. Past this the user is staring at a spinner, and one extra call
# to a courtesy limit is the lesser harm.
SHARED_SLOT_MAX_WAIT_S = 5.0

# Nominatim's published policy: max 1 request/second from an application —
# hence a fleet-wide slot, not just a per-process interval.
_nominatim_limiter = AsyncRateLimiter(1.1, shared_key="nominatim")


async def _get_or_fetch_async(key: str, fetch, *, offline: bool) -> Any:
    """Async twin of :func:`_get_or_fetch`.

    Cache reads and writes are small local file operations, so they stay
    synchronous; only the network call is awaited.
    """
    cached = load_cached(key)
    if cached is not None:
        return cached
    if offline:
        raise OpenDataError(
            f"No cached data for {key!r} and running offline. "
            f"Re-run the importer without --offline to fetch it."
        )
    payload = await fetch()
    save_cached(key, payload)
    return payload


def overpass(query: str, *, key: str, offline: bool = False) -> dict:
    """Run an Overpass QL query against OpenStreetMap.

    Overpass is a free, shared, donation-funded service that rate-limits by IP.
    Backing off properly isn't defensive coding here so much as basic manners —
    and the cache means a given query is only ever run once.
    """

    def fetch() -> dict:
        delay = 5.0
        for attempt in range(1, 5):
            logger.info("Overpass query %s (attempt %d)", key, attempt)
            with httpx.Client(timeout=180, headers={"User-Agent": USER_AGENT}) as c:
                r = c.post(OVERPASS_URL, data={"data": query})
                if r.status_code in (429, 504):
                    logger.warning(
                        "  rate-limited (%d), waiting %.0fs", r.status_code, delay
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                r.raise_for_status()
                # Space out successive queries even on success.
                time.sleep(2.0)
                return r.json()
        raise OpenDataError(
            f"Overpass kept rate-limiting {key!r}. Try again in a few minutes."
        )

    return _get_or_fetch(key, fetch, offline=offline)


def lagos_lgas(*, offline: bool = False) -> list[dict]:
    """The 20 Lagos State LGAs with centroids, from OSM admin boundaries."""
    query = """
    [out:json][timeout:90];
    area["name"="Lagos"]["admin_level"="4"]->.lagos;
    relation(area.lagos)["admin_level"="6"]["boundary"="administrative"];
    out tags center;
    """
    data = overpass(query, key="osm_lagos_lgas", offline=offline)
    out = []
    for e in data.get("elements", []):
        name = e.get("tags", {}).get("name")
        centre = e.get("center")
        if name and centre:
            out.append({"name": name, "lat": centre["lat"], "lng": centre["lon"]})
    return sorted(out, key=lambda x: x["name"])


def lagos_neighbourhoods(*, offline: bool = False) -> list[dict]:
    """Named suburbs and neighbourhoods within Lagos State."""
    query = """
    [out:json][timeout:90];
    area["name"="Lagos"]["admin_level"="4"]->.lagos;
    (
      node(area.lagos)["place"~"^(suburb|neighbourhood|quarter)$"]["name"];
      way(area.lagos)["place"~"^(suburb|neighbourhood)$"]["name"];
    );
    out tags center 400;
    """
    data = overpass(query, key="osm_lagos_neighbourhoods", offline=offline)
    out = []
    for e in data.get("elements", []):
        tags = e.get("tags", {})
        centre = e.get("center") or ({"lat": e.get("lat"), "lon": e.get("lon")})
        if tags.get("name") and centre.get("lat") is not None:
            out.append(
                {
                    "name": tags["name"],
                    "place": tags.get("place"),
                    "lat": centre["lat"],
                    "lng": centre["lon"],
                }
            )
    return out


def transit_near(
    lat: float, lng: float, *, key: str, offline: bool = False
) -> list[dict]:
    """Bus stops, BRT, ferry terminals and rail stations near a point.

    OSM's Lagos transit coverage is patchy — informal danfo stops are largely
    unmapped — so a thin result is a real finding about the data, not a bug.
    """
    query = f"""
    [out:json][timeout:90];
    (
      node(around:1200,{lat},{lng})["highway"="bus_stop"];
      node(around:1500,{lat},{lng})["amenity"="bus_station"];
      node(around:3000,{lat},{lng})["amenity"="ferry_terminal"];
      node(around:3000,{lat},{lng})["railway"="station"];
    );
    out tags center 60;
    """
    data = overpass(query, key=key, offline=offline)
    out = []
    for e in data.get("elements", []):
        tags = e.get("tags", {})
        if e.get("lat") is None:
            continue
        kind = (
            "ferry"
            if tags.get("amenity") == "ferry_terminal"
            else "rail"
            if tags.get("railway") == "station"
            else "bus"
        )
        out.append(
            {
                "kind": kind,
                "name": tags.get("name") or f"Unnamed {kind} stop",
                "lat": e["lat"],
                "lng": e["lon"],
                "distance_m": round(haversine_m(lat, lng, e["lat"], e["lon"])),
            }
        )
    out.sort(key=lambda x: x["distance_m"])

    # OSM contains duplicate nodes for the same real-world stop, often differing
    # only in capitalisation — Lekki's ferry terminal is mapped twice. Keep the
    # nearest of each name, and drop unnamed stops that sit on top of a named
    # one.
    deduped: list[dict] = []
    for item in out:
        name_key = item["name"].strip().lower()
        if any(d["name"].strip().lower() == name_key for d in deduped):
            continue
        if any(
            d["kind"] == item["kind"]
            and haversine_m(d["lat"], d["lng"], item["lat"], item["lng"]) < 120
            for d in deduped
        ):
            continue
        deduped.append(item)
    return deduped


def elevations(points: list[tuple[float, float]], *, offline: bool = False) -> list[float]:
    """Ground elevation in metres for each point (SRTM via Open-Elevation).

    This is the objective half of flood risk: Lagos floods where the land is at
    or near sea level, and that fact is measurable rather than reported.
    """
    key = "elevation_" + "_".join(f"{a:.4f},{b:.4f}" for a, b in points)[:80]

    def fetch() -> dict:
        locs = "|".join(f"{a},{b}" for a, b in points)
        logger.info("Elevation lookup for %d points", len(points))
        with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as c:
            r = c.get(ELEVATION_URL, params={"locations": locs})
            r.raise_for_status()
            return r.json()

    data = _get_or_fetch(key, fetch, offline=offline)
    return [float(p["elevation"]) for p in data.get("results", [])]


def _geocode_key(term: str) -> str:
    return "geocode_" + "".join(c if c.isalnum() else "_" for c in term.lower())[:60]


def _geocode_params(term: str, limit: int) -> dict[str, str]:
    return {
        "q": term,
        "format": "jsonv2",
        "limit": str(limit),
        "addressdetails": "1",
        # Lagos State bounding box: west,north,east,south. Bounding the search
        # also stops "Admiralty Way" resolving to a street in another country.
        "viewbox": "2.70,6.75,4.35,6.35",
        "bounded": "1",
        "countrycodes": "ng",
    }


def _shape_geocode(raw: list[dict]) -> list[dict]:
    out = []
    for item in raw:
        addr = item.get("address", {})
        out.append(
            {
                "label": item.get("display_name", ""),
                "lat": float(item["lat"]),
                "lng": float(item["lon"]),
                "road": addr.get("road"),
                "suburb": addr.get("suburb")
                or addr.get("neighbourhood")
                or addr.get("quarter"),
                "city": addr.get("city") or addr.get("town") or addr.get("state"),
                "type": item.get("type"),
            }
        )
    return out


async def geocode_async(
    query: str, *, limit: int = 6, offline: bool = False
) -> list[dict]:
    """Look up a Lagos address without blocking the event loop.

    This is the version request handlers must use. The synchronous :func:`geocode`
    below is for the offline importer and for tests; calling it from an
    ``async def`` handler stalls every other request in the process, because
    both the HTTP call and the courtesy delay are blocking.
    """
    term = " ".join(query.strip().split())
    if len(term) < 3:
        return []

    async def fetch() -> list[dict]:
        # Wait for our slot on the loop rather than sleeping through it.
        await _nominatim_limiter.acquire()
        # The term is a user's typed home address. Logged by length only —
        # enough to see that a lookup happened and roughly how specific it was,
        # without writing where somebody lives into a file that gets shipped to
        # a log aggregator and kept for months.
        logger.info("Nominatim search (%d chars)", len(term))
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": USER_AGENT}
        ) as c:
            r = await c.get(NOMINATIM_URL, params=_geocode_params(term, limit))
            r.raise_for_status()
            return r.json()

    raw = await _get_or_fetch_async(_geocode_key(term), fetch, offline=offline)
    return _shape_geocode(raw)


# Roughly the radius of a Lagos neighbourhood. A street match further than this
# from the named area is a different street that happens to share a name.
# Measured case: "Salako Street" in Ogba sits 5.3km from Magodo — close enough
# to look plausible, far enough to be somebody else's street. Only street-tier
# matches (where the area word was dropped to get a hit) are filtered; a
# full-string match already carries its own area context.
AREA_ANCHOR_RADIUS_M = 3_500

# A suffix starting with one of these is the tail of a street name, not an area:
# splitting "Salako Street Magodo" after "Salako" would ask the geocoder to find
# an area called "Street Magodo".
STREET_TYPE_WORDS = {
    "street",
    "st",
    "road",
    "rd",
    "close",
    "cl",
    "avenue",
    "ave",
    "way",
    "crescent",
    "cres",
    "drive",
    "lane",
    "boulevard",
    "blvd",
    "expressway",
    "expwy",
    "highway",
    "hwy",
}


def _strip_house_number(words: list[str]) -> list[str]:
    """Drop a leading house number. OSM has almost none for Lagos."""
    body = list(words)
    # "No" / "No." / "#" may stand as their own token before the digits.
    while body and re.fullmatch(
        r"(?:no\.?|#)\d*[a-z]?|\d+[a-z]?", body[0], re.IGNORECASE
    ):
        body = body[1:]
    return body or list(words)


def split_street_and_area(query: str) -> list[tuple[str, str]]:
    """Every plausible (street, area) split of an address, best guess first.

    Lagos area names are frequently multi-word — "Magodo Phase 1", "Lekki Phase
    1", "Ikeja GRA", "Victoria Island" — so the area is a *suffix* of unknown
    length, not the last token. Taking the last token alone produced two
    failures on real input: "16 salako street magodo phase 1" yielded the area
    "1", and "phase 1" on its own geocodes to a housing estate in Eti-Osa,
    nowhere near Magodo.

    Splits are ordered longest-area-first, because the longer suffix is the more
    specific place, and a suffix that begins with a street-type word (or is only
    digits) is not an area name at all.
    """
    body = _strip_house_number([w for w in re.split(r"[,\s]+", query.strip()) if w])
    splits: list[tuple[str, str]] = []
    # k is where the area starts; a longer area suffix is tried first.
    for k in range(1, len(body)):
        area = body[k:]
        if area[0].lower() in STREET_TYPE_WORDS:
            continue
        if all(re.fullmatch(r"\d+[a-z]?", w, re.IGNORECASE) for w in area):
            continue
        splits.append((" ".join(body[:k]), " ".join(area)))
    splits.sort(key=lambda s: -len(s[1].split()))
    return splits


def address_variants(query: str) -> list[tuple[str, str]]:
    """Progressively broader forms of an address, each with a precision label.

    OSM has thin street-level coverage in Lagos and almost no house numbers, so
    a single all-or-nothing lookup fails on ordinary addresses. Measured:

        "16 salako street magodo"  -> 0 hits
        "salako street magodo"     -> 0 hits
        "Salako Street"            -> 2 hits
        "Magodo"                   -> 1 hit

    A tenant typing their real address got "no match" and a dead end. So we
    widen the query in steps and report which step succeeded, rather than
    pretending an area-level hit is the building.
    """
    words = [w for w in re.split(r"[,\s]+", query.strip()) if w]
    if not words:
        return []

    variants: list[tuple[str, str]] = [(" ".join(words), "exact")]

    body = _strip_house_number(words)
    if body != words:
        variants.append((" ".join(body), "exact"))

    splits = split_street_and_area(query)
    variants += [(street, "street") for street, _ in splits if street]
    variants += [(area, "area") for _, area in splits]

    # Preserve order, drop duplicates and fragments too short to be meaningful.
    seen: set[str] = set()
    out = []
    for term, precision in variants:
        key = term.lower()
        if key not in seen and len(term) >= 3:
            seen.add(key)
            out.append((term, precision))
    return out


async def geocode_progressive(
    query: str,
    *,
    limit: int = 6,
    offline: bool = False,
    anchor: tuple[float, float] | None = None,
) -> tuple[list[dict], str]:
    """Widen the search until something matches. Returns (hits, precision).

    ``precision`` is ``exact`` | ``street`` | ``area`` | ``none`` and is meant
    to be shown to the user — an area-level pin is useful for registering a
    building, but only if nobody is told it is the building.

    ``anchor`` is a known point for the area named in the query. Callers that
    can resolve the area from local data should pass it: it is more reliable
    than geocoding a guessed suffix, and it saves a network round trip.
    """
    variants = address_variants(query)
    if not variants:
        return [], "none"

    # Resolve the area first and use it as an anchor. Lagos reuses street names
    # across the city — "Salako Street" exists in Ogba and elsewhere — so a
    # street-tier match found after dropping the area can easily be a street of
    # the right name in the wrong place. Pinning a tenant 12km from home is a
    # worse failure than admitting we only know the area.
    area_hits: list[dict] = []
    for term, precision in variants:
        if precision != "area":
            continue
        area_hits = await geocode_async(term, limit=limit, offline=offline)
        if area_hits:
            if anchor is None:
                anchor = (area_hits[0]["lat"], area_hits[0]["lng"])
            break

    for term, precision in variants:
        if precision == "area":
            continue
        hits = await geocode_async(term, limit=limit, offline=offline)
        if not hits:
            continue
        if anchor and precision == "street":
            near = [
                h
                for h in hits
                if haversine_m(anchor[0], anchor[1], h["lat"], h["lng"])
                <= AREA_ANCHOR_RADIUS_M
            ]
            if not near:
                # Right street name, wrong side of Lagos. Fall through to the
                # area, which at least is where they said they live.
                continue
            hits = near
        return hits, precision

    if area_hits:
        return area_hits, "area"
    return [], "none"


def geocode(query: str, *, limit: int = 6, offline: bool = False) -> list[dict]:
    """Blocking address lookup, for the importer CLI and tests.

    Request handlers want :func:`geocode_async`.
    """
    term = " ".join(query.strip().split())
    if len(term) < 3:
        return []

    def fetch() -> list[dict]:
        # The term is a user's typed home address. Logged by length only —
        # enough to see that a lookup happened and roughly how specific it was,
        # without writing where somebody lives into a file that gets shipped to
        # a log aggregator and kept for months.
        logger.info("Nominatim search (%d chars)", len(term))
        with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as c:
            r = c.get(NOMINATIM_URL, params=_geocode_params(term, limit))
            r.raise_for_status()
            time.sleep(1.1)  # honour the 1 req/s policy
            return r.json()

    raw = _get_or_fetch(_geocode_key(term), fetch, offline=offline)
    return _shape_geocode(raw)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    from math import asin, cos, radians, sin, sqrt

    r = 6_371_000.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return 2 * r * asin(sqrt(a))


def flood_zone_from_elevation(elevation_m: float, drainage_dist_m: float | None) -> str:
    """Classify flood exposure from ground height and drainage proximity.

    Lagos flooding is driven by elevation above sea level far more than by
    rainfall — the lagoon-side lowlands go under every rainy season. Thresholds
    follow the NIHSA Annual Flood Outlook's coastal-lowland banding.
    """
    if elevation_m <= 2:
        return "VeryHigh"
    if elevation_m <= 5:
        return "High" if (drainage_dist_m or 999) < 150 else "Moderate"
    if elevation_m <= 15:
        return "Moderate"
    return "Low"


# A name that is only digits is a house number OSM recorded without a street —
# "28", "60/61". Importing those as properties would fill the map with buildings
# called "28" that no tenant could recognise or search for.
_NUMERIC_NAME_RE = re.compile(r"^[\d\s/,.-]+$")

# Words that mean the feature is a *place* rather than a building someone rents
# a flat in. These already arrive through `lagos_neighbourhoods`.
_AREA_WORDS = {"estate", "scheme", "layout", "gra", "phase", "village", "town"}

# Buildings nobody rents a flat in, and which should not be soliciting public
# reviews at all. Diplomatic residences are the sharp case: OSM names them after
# the post's occupant, so importing one would invite the internet to review a
# named individual's home. Barracks and government housing are institutional —
# a tenant review model does not apply to them.
_NOT_RENTABLE_RE = re.compile(
    r"residence of|high commission|embassy|consulate|state house|"
    r"government house|barracks|police college|military|naval|army|prison",
    re.IGNORECASE,
)


def lagos_residential_buildings(*, offline: bool = False) -> list[dict]:
    """Named residential buildings and estates across Lagos, from OSM.

    This is the honest half of "seed real properties". A building's name and
    location are objective, openly licensed facts. What tenants paid, whether
    it floods and what the landlord is like are not facts OSM holds, and they
    stay empty until a tenant reports them.

    Selection is by *name*, not by OSM tag. The tagging in Lagos is
    inconsistent — Niger Towers and Titanium Towers are tagged
    ``landuse=residential`` while dozens of ``building=house`` features are
    named "28" or "60/61" — so trusting the tag would import the house numbers
    and skip the towers.
    """
    query = """
    [out:json][timeout:180];
    area["name"="Lagos"]["admin_level"="4"]->.lagos;
    (
      way(area.lagos)["building"~"^(apartments|residential|dormitory)$"]["name"];
      way(area.lagos)["landuse"="residential"]["name"];
      way(area.lagos)["residential"="gated"]["name"];
      relation(area.lagos)["landuse"="residential"]["name"];
    );
    out tags center 1200;
    """
    data = overpass(query, key="osm_lagos_residential", offline=offline)

    out: list[dict] = []
    seen: set[str] = set()
    for e in data.get("elements", []):
        tags = e.get("tags", {})
        name = (tags.get("name") or "").strip()
        centre = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if not name or centre.get("lat") is None:
            continue
        if _NUMERIC_NAME_RE.match(name) or len(name) < 3:
            continue
        if _NOT_RENTABLE_RE.search(name):
            continue

        key = f"{name.lower()}|{centre['lat']:.4f},{centre['lon']:.4f}"
        if key in seen:
            continue
        seen.add(key)

        words = {w.strip(".,").lower() for w in name.split()}
        out.append(
            {
                "name": name,
                "lat": centre["lat"],
                "lng": centre["lon"],
                "street": tags.get("addr:street"),
                "housenumber": tags.get("addr:housenumber"),
                # Recorded rather than filtered on: an estate is still somewhere
                # a tenant lives, but the caller may want to treat it as less
                # precise than a single named block.
                "is_area": bool(words & _AREA_WORDS),
                "osm_kind": tags.get("building") or tags.get("landuse") or "residential",
            }
        )
    return sorted(out, key=lambda r: r["name"])
