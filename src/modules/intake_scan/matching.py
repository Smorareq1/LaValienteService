"""Which customer this ticket is for (Plan 0003 §7.5).

A **suggestion**, never a decision, and never an auto-creation. The screen asks
"¿Es María López, 5512-3456?" and the person answers; getting it wrong silently
would file somebody's laundry under a stranger's name and, worse, hand them
somebody else's history.

Pure scoring: the caller does the queries and passes candidates in, so the rule
that decides who is the same person can be read and tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from src.modules.customers.models import Customer
from src.modules.intake_scan.normalization import normalize_name, normalize_phone

#: Below this a name match is not offered at all. Set high on purpose: the cost
#: of a wrong suggestion someone accepts in a hurry is much greater than the cost
#: of typing a name that was already in the system.
NAME_THRESHOLD = 0.82


@dataclass(frozen=True)
class Match:
    customer: Customer
    score: float
    matched_on: str


def name_similarity(left: str, right: str) -> float:
    """How alike two names are, ignoring case, accents and word order.

    Word order matters here more than it looks: the counter writes «López
    María» as often as «María López», and a plain string ratio would call those
    two different people.
    """
    left_words = sorted(normalize_name(left).split())
    right_words = sorted(normalize_name(right).split())
    if not left_words or not right_words:
        return 0.0
    return SequenceMatcher(
        None, " ".join(left_words), " ".join(right_words)
    ).ratio()


def best_match(
    *,
    phone: str | None,
    full_name: str | None,
    by_phone: Customer | None,
    candidates: list[Customer],
) -> Match | None:
    """The customer to suggest, if any.

    The phone wins whenever there is one. It is the only field on the ticket that
    is a real identifier — two people share a name, nobody shares a number — and
    an exact match on it is the same signal Plan 0004 §8 uses to spot a duplicate.
    """
    if phone and by_phone is not None:
        return Match(customer=by_phone, score=1.0, matched_on="phone")

    if not full_name:
        return None

    scored = [
        (name_similarity(full_name, candidate.full_name), candidate)
        for candidate in candidates
    ]
    scored = [pair for pair in scored if pair[0] >= NAME_THRESHOLD]
    if not scored:
        return None

    # Ties broken by the older row: if the shop registered the same person twice,
    # the first one is the one with the history attached.
    score, customer = max(scored, key=lambda pair: (pair[0], -pair[1].created_at.timestamp()))
    return Match(customer=customer, score=round(score, 3), matched_on="name")


def phone_for_lookup(raw_phone: str | None) -> str | None:
    """The digits to query by, or `None` if the reading was not a phone."""
    return normalize_phone(raw_phone)
