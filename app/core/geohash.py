"""Geohash encoding for the Property Identity System.

Lagos addresses are informal and ambiguous, so the *primary* identity signal is
the building's GPS centroid (from a map pin drop), not a typed address. A geohash
turns that lat/lng into a short, hierarchical, prefix-searchable string.

We need two precisions (see Section II of the spec):
  * precision-8  (~38m x 19m cell)  -> stored as ``geohash_8`` for fine dedup
  * precision-7  (~153m x 153m cell)-> stored as ``geohash_7`` for coarse buckets

The PropertyID embeds the first 6 characters of the precision-8 geohash as its
``GPS_HASH`` segment.

This is a self-contained, dependency-free implementation of the standard
Gustavo Niemeyer geohash algorithm so the identity layer has no external deps and
can be unit-tested in isolation.
"""

from __future__ import annotations

# Standard geohash base-32 alphabet (note: a, i, l, o are excluded).
_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE_MAP = {ch: i for i, ch in enumerate(_BASE32)}

LAT_RANGE = (-90.0, 90.0)
LNG_RANGE = (-180.0, 180.0)


def encode(lat: float, lng: float, precision: int = 8) -> str:
    """Encode a latitude/longitude into a geohash of the given precision.

    >>> encode(6.4281, 3.4219, 8)  # Lekki Phase 1, Lagos
    's1ztgd...'  (deterministic for a fixed input)
    """
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude out of range: {lat}")
    if not -180.0 <= lng <= 180.0:
        raise ValueError(f"longitude out of range: {lng}")
    if precision < 1:
        raise ValueError("precision must be >= 1")

    lat_lo, lat_hi = LAT_RANGE
    lng_lo, lng_hi = LNG_RANGE

    geohash: list[str] = []
    bits = 0
    bit_count = 0
    even_bit = True  # alternate between longitude (even) and latitude (odd)

    while len(geohash) < precision:
        if even_bit:
            mid = (lng_lo + lng_hi) / 2
            if lng >= mid:
                bits = (bits << 1) | 1
                lng_lo = mid
            else:
                bits = bits << 1
                lng_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                bits = (bits << 1) | 1
                lat_lo = mid
            else:
                bits = bits << 1
                lat_hi = mid

        even_bit = not even_bit
        bit_count += 1

        if bit_count == 5:
            geohash.append(_BASE32[bits])
            bits = 0
            bit_count = 0

    return "".join(geohash)


def decode(geohash: str) -> tuple[float, float]:
    """Decode a geohash to the centroid (lat, lng) of its cell."""
    lat_lo, lat_hi = LAT_RANGE
    lng_lo, lng_hi = LNG_RANGE
    even_bit = True

    for ch in geohash.lower():
        if ch not in _DECODE_MAP:
            raise ValueError(f"invalid geohash character: {ch!r}")
        idx = _DECODE_MAP[ch]
        for bit_pos in range(4, -1, -1):
            bit = (idx >> bit_pos) & 1
            if even_bit:
                mid = (lng_lo + lng_hi) / 2
                if bit:
                    lng_lo = mid
                else:
                    lng_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even_bit = not even_bit

    return ((lat_lo + lat_hi) / 2, (lng_lo + lng_hi) / 2)


def encode_pair(lat: float, lng: float) -> tuple[str, str]:
    """Return the (geohash_8, geohash_7) pair stored on the property record.

    geohash_7 is always a prefix of geohash_8, which is what lets us do cheap
    coarse-then-fine candidate lookups without PostGIS for the first pass.
    """
    gh8 = encode(lat, lng, 8)
    return gh8, gh8[:7]


def gps_hash(lat: float, lng: float) -> str:
    """The 6-character ``GPS_HASH`` segment embedded in a PropertyID.

    Truncated precision-8 geohash, uppercased to match the PropertyID format
    ``ETI-LEK-7F3A2B-0041``.
    """
    return encode(lat, lng, 8)[:6].upper()
