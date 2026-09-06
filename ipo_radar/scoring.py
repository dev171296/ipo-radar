"""
The quant scorer — turning the evidence bundle into numbers.

No AI in this file. This is arithmetic, and arithmetic does not make things up.
The AI gets its own separate score later, and the two are kept apart on purpose
so we can see when they disagree.

Two blocks, per the agreed model:

  F — Fundamentals: is this a good business at this price?
  D — Demand:       does the market want it?

Both come out of 100.

Three rules run through everything here
---------------------------------------
1. **A curve is an opinion, so every curve is written down.** Turning "revenue
   grew 53%" into "11 points out of 14" needs a rule, and that rule smuggles in
   a view of the world. Each one below states its reasoning in plain English so
   you can disagree with it. Where a number is provisional (a market norm we
   have not yet measured ourselves) it says so.

2. **Missing is not zero.** If we have no grey market premium, that component is
   dropped and the score is worked out over what remains, with the coverage
   reported. Scoring an absent GMP as zero would quietly mark every IPO down.

3. **Extreme is suspicious, not excellent.** A 170% profit growth rate scores
   well AND raises a flag. It is either a very good business or an accounting
   story, and numbers alone cannot tell the two apart — the prospectus chapters
   can, which is the AI stage's job.
"""

import json
import os
import re

from . import storage
from .collectors.bhavcopy import india_now

SCORES = os.path.join(storage.DATA, "scores")


# ------------------------------------------------------------------ curves

def band(value, steps, above):
    """
    Turn a number into a fraction between 0 and 1 using written-down steps.

    `steps` is a list of (threshold, fraction) read in order: the first
    threshold the value falls under decides the fraction. `above` is what it
    gets if it clears every threshold. Deliberately blunt — a step chart is
    honest about being a judgement, where a smooth formula pretends to precision
    it does not have.
    """
    if value is None:
        return None
    for threshold, fraction in steps:
        if value < threshold:
            return fraction
    return above


def _value(section, name):
    body = (section or {}).get(name)
    return body.get("value") if isinstance(body, dict) else None


# ------------------------------------------------- F — the fundamentals block

def cash_flow_quality(bundle, flags):
    """
    14 points. Did the profit arrive as money?

    Reasoning: profit is an opinion, cash is a fact. A sale is booked when the
    goods go out, whether or not the customer ever pays — so profit can climb for
    years while the bank balance does not. The measure is **cash conversion**:
    operating cash flow divided by EBITDA. Around 1.0 means the reported profit
    turned into money. Well below it means the profit is sitting in someone
    else's account, usually as unpaid invoices.

    A negative latest year is capped hard regardless of the ratio: a business
    that consumed cash in the year it chose to list is a different proposition
    from one that merely converted poorly.
    """
    cash = bundle.get("cash") or {}
    ratio = _value(cash, "conversion_vs_ebitda")
    basis = "EBITDA"
    if ratio is None:
        ratio, basis = _value(cash, "conversion_vs_pat"), "profit after tax"
    if ratio is None:
        return None, "no cash flow statement read"

    fraction = band(ratio, [(0, 0.0), (0.25, 0.15), (0.50, 0.35),
                            (0.75, 0.60), (1.00, 0.85)], above=1.0)

    flow = _value(cash, "operating_cash_flow") or []
    if _value(cash, "negative_in_all_years"):
        fraction = 0.0
        flags.append({"flag": "cash was consumed every year",
                      "detail": f"operating cash flow {flow}",
                      "for_ai": "veto — how is the business funded?"})
    elif _value(cash, "negative_in_latest_year"):
        fraction = min(fraction, 0.15)
        flags.append({
            "flag": "the business consumed cash in its latest year",
            "detail": f"operating cash flow {flow[0]} against a reported profit",
            "for_ai": "where did the money go — receivables, inventory, or both?"})
    elif ratio < 0.4:
        flags.append({
            "flag": "profit is not turning into cash",
            "detail": f"only {ratio * 100:.0f}% of {basis} came through as cash",
            "for_ai": "check receivables and inventory against the sales growth"})

    share = _value(cash, "receivables_share_of_sales_growth")
    if share and share > 0.5:
        fraction = min(fraction, 0.5)
        flags.append({
            "flag": "sales growth is largely uncollected",
            "detail": f"{share * 100:.0f}% of the increase in sales is money "
                      f"customers have not paid",
            "for_ai": "veto check — receivables growing faster than sales"})

    return fraction, f"cash conversion {ratio:.2f}× of {basis}"


def valuation_vs_peers(bundle, flags):
    """
    18 points, the heaviest thing in the fundamentals block. Is the price fair?

    Reasoning: everything else in F asks whether this is a good business. This
    asks what you are being charged for it, and a good business at a silly price
    is a bad investment. The comparison is the multiple being asked (top of the
    band ÷ latest earnings per share) against the peer group in the company's own
    prospectus — its own chosen yardstick, so it cannot complain about the
    comparison.

    Two honesty adjustments:
      * A discount scores well, but a discount measured against a yardstick we
        could not verify (a stated average, or a single peer) is pulled back
        toward neutral. A confident answer from a weak yardstick is worse than a
        cautious one.
      * A very large discount is flagged as well as rewarded. The market may be
        pricing the peers, not this company, and the peer set is chosen by the
        seller.
    """
    priced = bundle.get("valuation") or {}
    ratio = _value(priced, "vs_benchmark")
    if ratio is None:
        return None, "no usable price comparison in the prospectus"

    fraction = band(ratio, [(0.50, 1.00), (0.75, 0.90), (1.00, 0.75),
                            (1.25, 0.50), (1.75, 0.30), (2.50, 0.15)], above=0.05)

    verified = _value(priced, "peers_verified") or 0
    if not verified:
        # Shrink the answer toward the middle when the yardstick is weak.
        fraction = 0.5 + (fraction - 0.5) * 0.7

    asked = _value(priced, "pe_at_cap_price")
    benchmark = _value(priced, "benchmark_pe")

    if ratio < 0.5:
        flags.append({
            "flag": "priced far below its stated peer group",
            "detail": f"{asked}× against {benchmark}× — a "
                      f"{(1 - ratio) * 100:.0f}% discount",
            "for_ai": "is the peer set fairly chosen, and are the peers "
                      "themselves expensive rather than this being cheap?"})
    elif ratio > 1.5:
        flags.append({
            "flag": "priced well above its stated peer group",
            "detail": f"{asked}× against {benchmark}× — a "
                      f"{(ratio - 1) * 100:.0f}% premium",
            "for_ai": "what justifies the premium — growth, margins, scarcity?"})

    note = f"{asked}× earnings vs {benchmark}×"
    note += (f", from {verified} verified peer(s)" if verified
             else ", against the document's stated average (unverified)")
    return fraction, note


def growth_quality(bundle, flags):
    """
    14 points. Is the business getting bigger, and did it get bigger honestly?

    Reasoning: in Indian IPOs a steady 20-35% climb is a stronger signal than a
    sudden leap. Sellers time an issue for the year after a jump, so the jump is
    often the reason the IPO exists rather than evidence of a good business.
    Growth above 60% therefore scores slightly BELOW the 35-60% band, and a
    revenue leap concentrated in the final year before listing costs points and
    raises a flag. (Provisional bands — to be recomputed from our own history
    once we have tracked enough issues.)
    """
    derived = bundle.get("derived") or {}
    cagr = _value(derived, "revenue_cagr_2y_pct")
    if cagr is None:
        return None, "no three-year revenue on file"

    fraction = band(cagr, [(0, 0.0), (10, 0.30), (20, 0.55),
                           (35, 0.80), (60, 1.00)], above=0.85)
    if cagr >= 60:
        flags.append({"flag": "very high revenue growth",
                      "detail": f"{cagr}% a year — scored just below the band "
                                f"beneath it, because a pre-IPO surge is as often "
                                f"a reason for the timing as a sign of quality",
                      "for_ai": "check whether the growth is organic and repeatable"})

    revenue = _value(bundle.get("financials") or {}, "revenue")
    if revenue and len(revenue) == 3 and revenue[1] and revenue[2]:
        latest_growth = (revenue[0] - revenue[1]) / revenue[1]
        earlier_growth = (revenue[1] - revenue[2]) / revenue[2]
        if latest_growth > 0.35 and earlier_growth < 0.10:
            fraction = min(fraction, 0.55)
            flags.append({
                "flag": "growth concentrated in the year before the IPO",
                "detail": f"sales moved {earlier_growth * 100:.0f}% then "
                          f"{latest_growth * 100:.0f}% — the good year is the one "
                          f"just before listing",
                "for_ai": "look for one-off orders, related-party sales or a "
                          "change in how revenue is recognised"})
        if revenue[0] > revenue[2] * 3:
            flags.append({"flag": "revenue more than tripled in two years",
                          "detail": f"{revenue[2]} to {revenue[0]}",
                          "for_ai": "veto check — is this explained in the RHP?"})

    return fraction, f"revenue CAGR {cagr}% a year"


def profitability(bundle, flags):
    """
    12 points. Does the growth turn into profit, and is that improving?

    Reasoning: margin LEVEL says what kind of business it is; margin DIRECTION
    says what is happening to it. A thin margin that is widening beats a fat one
    that is shrinking, so direction can move the score by a fifth either way.
    ROE above 50% is flagged rather than praised — before an IPO a company often
    has very little equity, which flatters the ratio without flattering the
    business.
    """
    derived = bundle.get("derived") or {}
    margins = _value(derived, "pat_margin_pct")
    if not margins:
        return None, "no profit and revenue pair on file"

    latest = margins[0]
    fraction = band(latest, [(0, 0.0), (3, 0.25), (6, 0.45),
                             (10, 0.65), (15, 0.85)], above=1.0)

    direction = None
    if len([m for m in margins if m is not None]) >= 2:
        if margins[0] > margins[-1]:
            fraction, direction = min(1.0, fraction * 1.2), "widening"
        elif margins[0] < margins[-1]:
            fraction, direction = fraction * 0.8, "narrowing"

    roe = _value(derived, "roe_pct_latest")
    if roe is not None:
        roe_fraction = band(roe, [(10, 0.2), (15, 0.4), (20, 0.6), (30, 0.85)],
                            above=1.0)
        fraction = (fraction * 2 + roe_fraction) / 3     # margin counts double
        if roe > 50:
            flags.append({"flag": "unusually high return on equity",
                          "detail": f"ROE {roe}% — often a sign of a very small "
                                    f"equity base before the issue rather than an "
                                    f"exceptional business",
                          "for_ai": "check the capital structure chapter"})

    if all(m is not None and m < 0 for m in margins):
        flags.append({"flag": "loss-making in every year shown",
                      "detail": f"margins {margins}",
                      "for_ai": "veto check"})

    note = f"latest profit margin {latest}%"
    if direction:
        note += f", {direction}"
    if roe is not None:
        note += f", ROE {roe}%"
    return fraction, note


def issue_structure(bundle, flags):
    """
    10 points. Does the money go INTO the company, or to the people leaving?

    Reasoning: a fresh issue funds the business; an offer for sale moves cash to
    existing owners and leaves the company exactly as it was. Neither is wrong —
    early investors are entitled to exit — but an issue that is almost entirely
    an exit deserves a harder look at why they are getting out now.
    """
    offer = bundle.get("offer") or {}
    fresh = (offer.get("fresh_issue_rs_million") or {})
    total = _value(offer, "issue_size_rs_million")

    if fresh.get("confidence") == "contradicted":
        return None, "the two sources disagree about the size of the issue"
    if not fresh.get("value") or not total:
        return None, "fresh issue and total size not both readable"

    share = fresh["value"] / total
    fraction = band(share, [(0.25, 0.25), (0.50, 0.50), (0.75, 0.80)], above=1.0)
    if share < 0.25:
        flags.append({"flag": "mostly an exit for existing holders",
                      "detail": f"only {share * 100:.0f}% of the money reaches "
                                f"the company",
                      "for_ai": "who is selling, and how much of their stake?"})
    return fraction, f"{share * 100:.0f}% of the issue is fresh money to the company"


def filing_hygiene(bundle, flags):
    """
    10 points. How complete and self-consistent is the paperwork?

    Reasoning: this scores US as much as the company — a missing document is our
    gap, not their failing. But it is honest to show it, because a score built on
    half the paperwork should not look as confident as one built on all of it.
    A contradiction between two sources costs real points.
    """
    documents = bundle.get("documents") or {}
    if not documents:
        return None, "no documents on file — nothing to judge the paperwork by"
    fraction = 0.0
    parts = []

    if "abridged" in documents:
        fraction += 0.35
        parts.append("summary form read")
    if "full" in documents:
        fraction += 0.35
        parts.append(f"{documents['full'].get('pages')}-page prospectus read")
    if len((documents.get("abridged") or {}).get("fields") or []) >= 10:
        fraction += 0.15
        parts.append("all key fields found")
    if bundle.get("conflicts"):
        fraction = max(0.0, fraction - 0.25)
        parts.append(f"{len(bundle['conflicts'])} contradiction(s)")
    else:
        fraction += 0.15
        parts.append("no contradictions")

    return min(1.0, fraction), "; ".join(parts)


# ------------------------------------------------------ D — the demand block

def subscription_level(bundle, flags):
    """
    26 points, the single heaviest component. How many times over was it bought?

    Reasoning: subscription is the one demand signal that is a fact rather than
    an opinion — real money, committed, reported by the exchange. The curve
    flattens above 15× because by then everyone knows it is hot and the extra
    multiples add little.

    **This is only scored once the issue has closed.** Indian IPOs fill from the
    back: the retail portion largely arrives on the final afternoon, so a day-one
    reading of 0.4× and a final reading of 0.4× mean completely different things
    and cannot share a curve. Before the close the figure is still recorded and
    still shown — it is simply not turned into points, and it is never called
    under-subscribed. An issue on day one has not failed to fill; it has not
    been given the chance.
    """
    times = _value(bundle.get("demand") or {}, "subscription_times")
    if times is None:
        return None, "no subscription figure yet"

    close = _value(bundle.get("offer") or {}, "close_date")
    today = india_now().date().isoformat()
    if close and today <= close:
        return None, (f"the issue is still open (closes {close}) and stands at "
                      f"{times}× — most demand arrives on the last day, so this "
                      f"is not scored yet")

    fraction = band(times, [(1, 0.10), (2, 0.30), (5, 0.50),
                            (15, 0.75), (50, 0.90)], above=1.0)
    if times < 1:
        flags.append({"flag": "under-subscribed",
                      "detail": f"closed at {times}× — the issue did not fill",
                      "for_ai": "this usually overrides a good fundamental story"})
    return fraction, f"closed subscribed {times}×"


def subscription_velocity(bundle, flags):
    """
    14 points. How FAST did the money arrive?

    Reasoning: an issue that reaches 3× on day one behaves very differently on
    listing day from one that crawls to 3× on the final afternoon. Early demand
    is conviction; late demand is often people piling in on the news that it is
    filling. We need at least two readings to say anything, so this scores as
    unavailable until the issue has been open a while.
    """
    history = _value(bundle.get("demand") or {}, "subscription_history") or []
    if len(history) < 2:
        return None, "need at least two readings to see a rate"
    if len(history) < 3:
        return None, (f"only {len(history)} readings so far — too few to call "
                      f"the pace")

    first, last = history[0], history[-1]
    if not last["times"]:
        return None, "no movement recorded yet"
    early_share = (first["times"] or 0) / last["times"]
    fraction = band(early_share, [(0.10, 0.25), (0.25, 0.50), (0.50, 0.80)],
                    above=1.0)
    return fraction, (f"{early_share * 100:.0f}% of final demand was already in "
                      f"at the first reading")


# ---------------------------------------------------------------- the blocks

FUNDAMENTALS = [
    ("valuation_vs_peers", 18, valuation_vs_peers),
    ("growth_quality", 14, growth_quality),
    ("cash_flow_quality", 14, cash_flow_quality),
    ("profitability", 12, profitability),
    ("promoter_governance", 12, None),     # needs the promoter chapter read
    ("balance_sheet", 10, None),           # needs the RHP financial statements
    ("issue_structure", 10, issue_structure),
    ("filing_hygiene", 10, filing_hygiene),
]

DEMAND = [
    ("subscription_level", 26, subscription_level),
    ("subscription_velocity", 14, subscription_velocity),
    ("gmp_level", 12, None),               # no free source yet
    ("gmp_trend", 10, None),
    ("anchor_quality", 12, None),          # not collected yet
    ("broker_conviction", 10, None),
    ("media_coverage", 8, None),
    ("retail_attention", 8, None),
]

NOT_BUILT = "not built yet"

# How much of a block must be assessable before its score means anything.
MINIMUM_COVERAGE_PCT = 25


def score_block(bundle, definition, flags):
    """
    Run one block's components and combine them over what was available.

    The renormalising is the important part: if only 46 of the 100 possible
    points could be assessed, the score is worked out over those 46 and reported
    with `coverage: 46%`. A score of 71 at 46% coverage is a different thing from
    a score of 71 at full coverage, and the number alone must never hide that.
    """
    scored, skipped, earned, possible = [], [], 0.0, 0

    for name, weight, worker in definition:
        if worker is None:
            skipped.append({"component": name, "weight": weight, "why": NOT_BUILT})
            continue
        fraction, note = worker(bundle, flags)
        if fraction is None:
            skipped.append({"component": name, "weight": weight, "why": note})
            continue
        points = round(fraction * weight, 2)
        earned += points
        possible += weight
        scored.append({"component": name, "weight": weight, "points": points,
                       "basis": note})

    total_weight = sum(w for _, w, _ in definition)
    coverage = possible / total_weight * 100 if total_weight else 0
    score = round(earned / possible * 100, 1) if possible else None

    # Below a quarter of the block assessed, a score is noise dressed as a
    # number. A company we know almost nothing about must not come out looking
    # merely "low" — it must come out as unscored, which is the truth.
    withheld = None
    if score is not None and coverage < MINIMUM_COVERAGE_PCT:
        withheld = (f"only {coverage:.0f}% of this block could be assessed — "
                    f"below the {MINIMUM_COVERAGE_PCT}% floor, so no score is given")
        score = None

    return {
        "score": score,
        "withheld": withheld,
        "points_earned": round(earned, 2),
        "points_assessed": possible,
        "coverage_pct": round(coverage),
        "components": scored,
        "not_assessed": skipped,
    }


# -------------------------------------------------------------------- vetoes

def run_vetoes(bundle):
    """
    Hard stops. A veto caps the long-term verdict at 45 no matter how good the
    rest looks — these are the patterns that have historically preceded the worst
    outcomes, and no amount of growth compensates for them.

    Only the ones our current evidence can actually test are run. The rest are
    listed as untested rather than quietly passed, because "we did not check" and
    "we checked and it was fine" are very different statements.
    """
    triggered, untested = [], []
    financials = bundle.get("financials") or {}

    pat = _value(financials, "pat")
    if pat and len(pat) == 3:
        if all(p is not None and p < 0 for p in pat):
            triggered.append({"veto": "loss-making in all three years", "detail": pat})
    else:
        untested.append("three years of profit not on file")

    revenue = _value(financials, "revenue")
    if revenue and len(revenue) == 3 and revenue[2]:
        if revenue[0] > revenue[2] * 3:
            triggered.append({
                "veto": "revenue more than tripled in two years without explanation",
                "detail": f"{revenue[2]} to {revenue[0]}",
                "note": "stands until the AI stage finds an explanation in the RHP"})
    else:
        untested.append("three years of revenue not on file")

    cash = bundle.get("cash") or {}
    if _value(cash, "operating_cash_flow"):
        if _value(cash, "negative_in_all_years"):
            triggered.append({"veto": "negative operating cash flow in all three years",
                              "detail": _value(cash, "operating_cash_flow")})
        share = _value(cash, "receivables_share_of_sales_growth")
        if share and share > 1.0:
            triggered.append({
                "veto": "receivables growing faster than sales",
                "detail": f"{share * 100:.0f}% of the sales increase is uncollected"})
    else:
        untested.append("operating cash flow — statement not read")

    for name in ("qualified or adverse audit opinion",
                 "SEBI or criminal proceedings against the promoters",
                 "more than half of revenue from one related party"):
        untested.append(name + " — needs the prospectus chapters read")

    return {"triggered": triggered, "not_tested": untested}


# ------------------------------------------------------------------ verdicts

def verdict(bundle: dict) -> dict:
    """
    Score one IPO from its evidence bundle.

    The two headline numbers are deliberately incomplete at this stage: the
    agreed model gives a quarter of the listing score and 30% of the long-term
    score to the AI block, which does not exist yet. So both are marked
    `quant_only` and must not be presented as final verdicts.
    """
    flags = []
    fundamentals = score_block(bundle, FUNDAMENTALS, flags)
    demand = score_block(bundle, DEMAND, flags)
    vetoes = run_vetoes(bundle)

    f, d = fundamentals["score"], demand["score"]
    listing = longterm = None
    if f is not None and d is not None:
        listing = round((0.25 * f + 0.50 * d) / 0.75, 1)   # AI's 25% left out
    if f is not None:
        longterm = round(f, 1)                             # AI's 30% left out
        if vetoes["triggered"]:
            longterm = min(longterm, 45.0)

    return {
        "ipo_id": bundle["ipo_id"],
        "name": bundle.get("name"),
        "scored_at": storage.now(),
        "built_from_bundle": bundle.get("built_at"),
        "fundamentals": fundamentals,
        "demand": demand,
        "flags": flags,
        "vetoes": vetoes,
        "listing_score_quant_only": listing,
        "longterm_score_quant_only": longterm,
        "status": "quant only — the AI block has not been built, so these are "
                  "not final verdicts",
    }


def save(result: dict) -> str:
    """Frozen, like the bundle. A score is a statement made at a moment."""
    folder = os.path.join(SCORES, result["ipo_id"])
    os.makedirs(folder, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", result["scored_at"])[:14]
    for target in (os.path.join(folder, f"{stamp}.json"),
                   os.path.join(folder, "latest.json")):
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=1, ensure_ascii=False)
    return os.path.join(folder, f"{stamp}.json")


def summarise(result: dict) -> str:
    fundamentals, demand = result["fundamentals"], result["demand"]
    def one(label, block):
        if block["score"] is None:
            return f"{label} not scored ({block['coverage_pct']}% covered)"
        return f"{label} {block['score']} ({block['coverage_pct']}% covered)"

    parts = [one("F", fundamentals), one("D", demand)]
    if result["vetoes"]["triggered"]:
        parts.append(f"{len(result['vetoes']['triggered'])} VETO")
    if result["flags"]:
        parts.append(f"{len(result['flags'])} flag(s)")
    return " | ".join(parts)
