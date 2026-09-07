"""
The call — what the system actually thinks you should do.

Everything else in this project measures. This is the one place that decides,
and it is deliberately the smallest, plainest file in the codebase, because a
decision rule you cannot read is a decision rule you cannot argue with.

Two calls, never one
--------------------
"Should I apply?" and "should I hold this for years?" are different questions
with different answers, and blending them produces advice that fits neither. A
richly subscribed issue at a silly price is often a good listing trade and a bad
investment; a dull issue in a sound business is the reverse. So:

  LISTING   — the first days of trading. Driven mostly by demand.
  LONG TERM — the business over years. Driven mostly by fundamentals.

How much each input counts
--------------------------
    Listing    = 25% fundamentals + 50% demand + 25% AI
    Long term  = 60% fundamentals + 30% AI, minus a hype penalty

...but only over the parts we actually have. If demand cannot be scored yet
because the issue has not opened, the listing call is worked out over what
remains, and says so. The alternative — treating an unknown as a zero — would
mark every issue "avoid" the day before it opens, which is precisely when you
would be reading this.

When we refuse to call it
-------------------------
Below 50% of the deciding weight available, there is no call at all. Not a
cautious one, not a neutral one — none. "I don't know yet" is a real answer and
the only honest one when half the evidence is missing.
"""

BANDS = [
    (80, "STRONG APPLY", "#1f7a4d"),
    (65, "APPLY", "#2f7d32"),
    (50, "APPLY SELECTIVELY", "#a86400"),
    (35, "WAIT FOR LISTING", "#b25b1a"),
    (0, "AVOID", "#a32d22"),
]

NO_CALL = ("NOT ENOUGH INFORMATION", "#6b6862")

# Findings that a good average must not be allowed to bury.
#
# This exists because of a real case: Steamhouse scored 81.8 on fundamentals —
# strong growth, excellent cash conversion, most of the money going into the
# business — and came out as STRONG APPLY. But its current ratio was 0.36: it
# owes nearly three times what it expects to collect within the year. Five good
# components outvoted one that could sink the company. A weighted average is the
# wrong tool for a condition that is fatal rather than merely bad, so any of
# these caps the long-term call at "apply selectively" and says why.
SERIOUS = {
    "cash was consumed every year",
    "the business consumed cash in its latest year",
    "owes more this year than it expects to collect",
    "sales growth is largely uncollected",
    "heavily indebted",
    "loss-making in every year shown",
}
SERIOUS_CEILING = 64          # the top of "apply selectively"

# Below this share of the deciding weight, we do not call it at all.
MINIMUM_WEIGHT = 0.50


def _band(score):
    for floor, label, colour in BANDS:
        if score >= floor:
            return label, colour
    return BANDS[-1][1], BANDS[-1][2]


def _weighted(pieces):
    """
    pieces: list of (weight, value, label). Missing values drop out and the
    remaining weights are rescaled, so a missing input dilutes confidence
    rather than dragging the score down.
    """
    have = [(w, v, l) for w, v, l in pieces if v is not None]
    total = sum(w for w, _, _ in have)
    if not total:
        return None, 0.0, []
    score = sum(w * v for w, v, _ in have) / total
    return round(score, 1), total, [l for _, _, l in have]


def decide(score: dict, comparison: dict = None) -> dict:
    """Turn our scoring and the two AI readings into two plain calls."""
    fundamentals = ((score or {}).get("fundamentals") or {}).get("score")
    demand = ((score or {}).get("demand") or {}).get("score")
    ai = (comparison or {}).get("ai_block") or {}
    vetoes = ((score or {}).get("vetoes") or {}).get("triggered") or []
    disagreements = (comparison or {}).get("disagreements") or []
    serious = [flag.get("flag") for flag in (score or {}).get("flags", [])
               if flag.get("flag") in SERIOUS]

    listing_score, listing_weight, listing_from = _weighted([
        (0.25, fundamentals, "our fundamentals"),
        (0.50, demand, "market demand"),
        (0.25, ai.get("listing_view"), "the two AI readings"),
    ])
    longterm_score, longterm_weight, longterm_from = _weighted([
        (0.60, fundamentals, "our fundamentals"),
        (0.30, ai.get("longterm_view"), "the two AI readings"),
    ])

    def wrap(name, value, weight, sources, ceiling=None, capped_by=None):
        if value is None or weight < MINIMUM_WEIGHT:
            return {
                "call": NO_CALL[0], "colour": NO_CALL[1], "score": value,
                "confidence": "none",
                "why": (f"only {weight * 100:.0f}% of what decides the "
                        f"{name} call is available — no call is better than a "
                        f"guess dressed as one"),
                "based_on": sources,
            }
        capped = value
        notes = []
        if ceiling is not None and value > ceiling:
            capped = ceiling
            notes.append(capped_by or f"capped at {ceiling}")

        label, colour = _band(capped)
        confidence = ("high" if weight >= 0.95 and not disagreements else
                      "low" if weight < 0.75 or disagreements else "medium")
        why = f"built from {', '.join(sources)}"
        if weight < 0.99:
            why += f" — {weight * 100:.0f}% of the deciding weight"
        if disagreements:
            why += "; the two models disagree, which lowers confidence"
        if notes:
            why += "; " + "; ".join(notes)
        return {"call": label, "colour": colour, "score": round(capped, 1),
                "confidence": confidence, "why": why, "based_on": sources}

    if vetoes:
        ceiling, capped_by = 45, ("capped at 45 by a hard stop: "
                                  + vetoes[0].get("veto", ""))
    elif serious:
        ceiling, capped_by = SERIOUS_CEILING, (
            f"held to '{_band(SERIOUS_CEILING)[0].lower()}' because of one "
            f"serious finding — {serious[0]} — which a good average must not "
            f"be allowed to bury")
    else:
        ceiling, capped_by = None, None

    return {
        "listing": wrap("listing", listing_score, listing_weight, listing_from),
        "longterm": wrap("long-term", longterm_score, longterm_weight,
                         longterm_from, ceiling=ceiling, capped_by=capped_by),
        "hard_stops": [v.get("veto") for v in vetoes],
        "serious_findings": serious,
        "models_disagree": bool(disagreements),
    }


def one_line(call: dict) -> str:
    """The whole thing in a sentence, for a subject line or a text digest."""
    listing = call["listing"]["call"]
    longterm = call["longterm"]["call"]
    if listing == longterm:
        return f"{listing} on both views"
    return f"{listing} for listing, {longterm} for the long term"
