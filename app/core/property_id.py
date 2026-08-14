"""PropertyID generation — the permanent, immutable identifier for a building.

Format (Section II):

    [LGA_CODE]-[AREA_CODE]-[GPS_HASH]-[SEQ]
    ETI-LEK-7F3A2B-0041

    ETI     Eti-Osa LGA (3-letter code from the ``lgas`` table)
    LEK     Lekki Phase 1 neighbourhood code (from the ``neighbourhoods`` table)
    7F3A2B  6-char truncated precision-8 geohash of the GPS centroid, uppercased
    0041    zero-padded sequence number disambiguating geohash collisions

The ID is derived purely from (lga_code, area_code, lat, lng, seq). The geohash
segment is a deterministic function of the GPS pin, so two submissions of the same
building land in the same geohash cell and only differ — if at all — by sequence.

Note on the spec example ``7F3A2B``: the spec calls GPS_HASH a "6-character
truncated geohash". The standard geohash alphabet is lowercase base-32 (no a/i/l/o),
which we uppercase for display. The literal example string is illustrative; real
values come from :func:`app.core.geohash.gps_hash`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import geohash

_LGA_RE = re.compile(r"^[A-Z]{2,5}$")
_AREA_RE = re.compile(r"^[A-Z]{2,10}$")
_PROPERTY_ID_RE = re.compile(
    r"^(?P<lga>[A-Z]{2,5})-(?P<area>[A-Z]{2,10})-(?P<hash>[0-9A-Z]{6})-(?P<seq>\d{4})$"
)

SEQ_WIDTH = 4
MAX_SEQ = 10**SEQ_WIDTH - 1  # 9999


@dataclass(frozen=True)
class ParsedPropertyId:
    lga_code: str
    area_code: str
    gps_hash: str
    seq: int

    @property
    def value(self) -> str:
        return format_property_id(self.lga_code, self.area_code, self.gps_hash, self.seq)


def format_property_id(lga_code: str, area_code: str, gps_hash: str, seq: int) -> str:
    """Assemble the canonical PropertyID string from its parts."""
    lga = lga_code.strip().upper()
    area = area_code.strip().upper()
    gh = gps_hash.strip().upper()

    if not _LGA_RE.match(lga):
        raise ValueError(f"invalid LGA code: {lga_code!r}")
    if not _AREA_RE.match(area):
        raise ValueError(f"invalid area code: {area_code!r}")
    if len(gh) != 6 or not gh.isalnum():
        raise ValueError(f"GPS hash must be 6 alphanumeric chars, got {gps_hash!r}")
    if not 0 <= seq <= MAX_SEQ:
        raise ValueError(f"sequence out of range [0, {MAX_SEQ}]: {seq}")

    return f"{lga}-{area}-{gh}-{seq:0{SEQ_WIDTH}d}"


def generate(lga_code: str, area_code: str, lat: float, lng: float, seq: int = 0) -> str:
    """Generate a PropertyID for a pin-dropped building.

    ``seq`` is supplied by the caller after a collision check: query existing
    properties sharing the same (lga, area, gps_hash) prefix and pass the next
    free sequence number. Defaults to 0 for the first property in a geohash cell.
    """
    gh = geohash.gps_hash(lat, lng)
    return format_property_id(lga_code, area_code, gh, seq)


def parse(property_id: str) -> ParsedPropertyId:
    """Parse a PropertyID string back into its components."""
    m = _PROPERTY_ID_RE.match(property_id.strip().upper())
    if not m:
        raise ValueError(f"malformed PropertyID: {property_id!r}")
    return ParsedPropertyId(
        lga_code=m.group("lga"),
        area_code=m.group("area"),
        gps_hash=m.group("hash"),
        seq=int(m.group("seq")),
    )


def prefix(lga_code: str, area_code: str, lat: float, lng: float) -> str:
    """The collision-bucket prefix ``LGA-AREA-GPSHASH`` (without the sequence).

    All properties whose pins fall in the same precision-8 geohash cell share this
    prefix; the sequence number disambiguates them.
    """
    gh = geohash.gps_hash(lat, lng)
    return f"{lga_code.strip().upper()}-{area_code.strip().upper()}-{gh}"


def next_seq(existing_ids: list[str]) -> int:
    """Given existing PropertyIDs in the same collision bucket, return next seq."""
    used = {parse(pid).seq for pid in existing_ids}
    seq = 0
    while seq in used:
        seq += 1
        if seq > MAX_SEQ:
            raise RuntimeError("geohash collision bucket exhausted (>9999)")
    return seq
