"""Perceptual hashing helpers for building-photo de-duplication.

Step 3 of the registration flow (Section II): a user can photograph the building
exterior. We compute a perceptual hash (pHash) so future submissions of the *same*
physical structure — different angle, lighting, or device — produce hashes within a
small Hamming distance, confirming it's the same building.

This module owns the distance/threshold logic and the hex<->bits handling so it can
be unit-tested without pulling in Pillow/imagehash. The actual pixel-level pHash is
computed in ``services.media.process_upload``, which runs off the event loop;
here we only need the comparison primitives, which is what the dedup query uses.
"""

from __future__ import annotations

# Two pHashes within this Hamming distance are treated as the same building
# (Section II, Step 5: "if Hamming distance <= 10, flag as potential duplicate").
DEFAULT_HAMMING_THRESHOLD = 10


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Hamming distance between two equal-length hex pHash strings.

    >>> hamming_distance("ffffffffffffffff", "ffffffffffffffff")
    0
    >>> hamming_distance("0", "f")
    4
    """
    a = hash_a.strip().lower()
    b = hash_b.strip().lower()
    if len(a) != len(b):
        raise ValueError("pHashes must be the same length to compare")
    xor = int(a, 16) ^ int(b, 16)
    return xor.bit_count()


def is_same_building(
    hash_a: str, hash_b: str, threshold: int = DEFAULT_HAMMING_THRESHOLD
) -> bool:
    """True if two pHashes are close enough to be the same physical building."""
    return hamming_distance(hash_a, hash_b) <= threshold
