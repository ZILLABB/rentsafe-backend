"""Automated review pre-checks — Section III moderation pipeline (step 2 & 3).

Pure, deterministic, DB-free so it's unit-testable. Returns a verdict the service
layer uses to route a submission: publish immediately, or hold for a human.

We deliberately keep the legally-risky patterns conservative: in Nigeria, calling
someone a "fraudster"/"thief" in a review is a defamation risk, so those route to
manual review rather than auto-publish (Section XV).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The app ships an English, Nigerian Pidgin and Yorùbá UI (Section XVII) and
# invites reviews in the writer's own language — so the filters have to cover all
# three. An English-only wordlist means the exact content this exists to catch
# passes straight through when written in Pidgin or Yorùbá.

# Strong profanity -> hold. Kept short and obvious; expand via config later.
_PROFANITY = {
    # English
    "fuck", "shit", "bastard", "idiot", "stupid",
    # Nigerian Pidgin / Yorùbá loan insults in common use
    "mumu", "ode", "olodo", "werey", "oloshi", "yeye", "mugu", "shege",
    "olóríburúkú", "oloriburuku", "aṣiwèrè", "asiwere",
}

# Legally risky third-party allegations -> always human review (Section XV).
_RISKY_PATTERNS = [
    # English
    r"\bfraud(?:ster)?\b",
    r"\bthief\b",
    r"\bcriminal\b",
    r"\bscammer?\b",
    r"\bcollected? and (?:vanished|disappeared)\b",
    r"\bdisappeared with (?:my |the )?money\b",
    r"\bdemanded? (?:a )?(?:kickback|bribe)\b",
    r"\bfake property\b",
    # Nigerian Pidgin — "419" is the penal-code section for fraud and is the
    # everyday word for it; "chop my money" / "carry my money go" are the
    # standard phrasings of an accusation of theft.
    r"\b419\b",
    r"\bna (?:thief|fraud|scam)\b",
    r"\bchop(?:ped)? my money\b",
    r"\bcarry (?:my|the) money (?:go|comot|run)\b",
    r"\bhe (?:no )?be (?:thief|fraudster|scammer)\b",
    r"\bdem (?:thief|scam|do me)\b",
    r"\bwaya(?:ed)? me\b",
    r"\bdupe(?:d)? me\b",
    # Yorùbá — olè (thief), jíjà/jí (steal), ẹ̀tàn/tàn jẹ (deceive, defraud).
    # Matched with and without diacritics: most Lagos keyboards omit them.
    r"\bol[eẹ][̀-ͯ]*\b",
    r"\bj[ií] owó\b",
    r"\bji owo\b",
    r"\b[eẹ][̀-ͯ]*t[aà]n\b",
    r"\betan\b",
    r"\bt[aà]n mi j[eẹ]\b",
    r"\btan mi je\b",
    r"\bjaguda\b",
    r"\bb[aà]r[aà]w[oò]\b",
    r"\bbarawo\b",
]
_RISKY_RE = re.compile("|".join(_RISKY_PATTERNS), re.IGNORECASE)

# Submissions per user in 24h above this are flagged for velocity (Section III).
VELOCITY_LIMIT_24H = 3


@dataclass
class ModerationResult:
    status: str  # "clean" | "flagged"
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.status == "flagged"


def _contains_profanity(text: str) -> bool:
    # `\w` with the default (unicode) flags keeps Yorùbá diacritics and
    # subdotted characters inside the token rather than splitting on them.
    words = set(re.findall(r"[\w'̀-ͯ]+", text.lower(), re.UNICODE))
    return bool(words & _PROFANITY)


def check_text(*texts: str | None) -> ModerationResult:
    """Run profanity + risky-language checks over one or more free-text fields."""
    reasons: list[str] = []
    blob = " \n ".join(t for t in texts if t)

    if _contains_profanity(blob):
        reasons.append("profanity")
    if _RISKY_RE.search(blob):
        reasons.append("legally_risky_language")

    return ModerationResult("flagged" if reasons else "clean", reasons)


def check_submission(
    *,
    texts: list[str | None],
    user_reviews_last_24h: int,
    duplicate_of_existing: bool = False,
) -> ModerationResult:
    """Full pre-check combining content, velocity and duplicate signals."""
    result = check_text(*texts)
    reasons = list(result.reasons)

    if user_reviews_last_24h >= VELOCITY_LIMIT_24H:
        reasons.append("velocity")
    if duplicate_of_existing:
        reasons.append("duplicate_text")

    return ModerationResult("flagged" if reasons else "clean", reasons)
