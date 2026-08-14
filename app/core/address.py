"""Address normalisation for Lagos's informal, inconsistent address strings.

Step 4 of the registration flow (Section II). The typed address is a *secondary*
signal — the pin drop is primary — but a normalised string lets us:
  * forward-geocode more reliably
  * fuzzy-match against existing records (pg_trgm on ``address_normalised``)
  * strip the descriptive noise Lagos addresses are full of
    ("Beside Chicken Republic, off Admiralty" -> "admiralty").

Normalisation is intentionally lossy and lowercase: it produces a comparison key,
not a display string. The original is preserved separately as ``address_formal``.
"""

from __future__ import annotations

import re

# Leading/standalone descriptive tokens that carry no positional value once we
# have a GPS pin. Order doesn't matter; matched as whole words, case-insensitive.
NOISE_TOKENS = {
    "no",
    "no.",
    "off",
    "beside",
    "behind",
    "opposite",
    "near",
    "by",
    "after",
    "before",
    "close to",
}

# Abbreviation -> canonical expansion. Applied as whole-word replacements.
ABBREVIATIONS = {
    "rd": "road",
    "ave": "avenue",
    "av": "avenue",
    "cl": "close",
    "st": "street",
    "str": "street",
    "cres": "crescent",
    "blvd": "boulevard",
    "hwy": "highway",
    "expwy": "expressway",
    "jcn": "junction",
    "jct": "junction",
    "est": "estate",
    "ph": "phase",
    "apt": "apartment",
    "bstp": "bus stop",
}

_PUNCT_RE = re.compile(r"[.,;:/\\]+")
_MULTISPACE_RE = re.compile(r"\s+")


def normalise(raw: str) -> str:
    """Return a lowercase, noise-stripped, abbreviation-expanded comparison key.

    >>> normalise("No. 12A, Off Admiralty Way, Beside Zenith Bank")
    '12a admiralty way zenith bank'
    >>> normalise("15 Adeola Odeku St.")
    '15 adeola odeku street'
    """
    if not raw:
        return ""

    text = raw.lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    text = _MULTISPACE_RE.sub(" ", text).strip()

    tokens = text.split(" ")
    out: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok in NOISE_TOKENS:
            continue
        out.append(ABBREVIATIONS.get(tok, tok))

    return _MULTISPACE_RE.sub(" ", " ".join(out)).strip()


def tokens(raw: str) -> list[str]:
    """Normalised tokens, useful for trigram/Jaccard comparison."""
    norm = normalise(raw)
    return norm.split(" ") if norm else []
