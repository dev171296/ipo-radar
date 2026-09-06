"""
The evidence bundle — everything we know about one IPO, in one file.

Why this exists
---------------
Our facts arrive from three different places: the NSE calendar (price band,
issue size, subscription), the abridged prospectus (three-year financials) and
the full prospectus (chapters). Scoring them straight from those places would
mean every later step re-reading, re-parsing and possibly re-interpreting them.

So we assemble ONE file per IPO per run, and everything downstream reads only
that file. Three consequences, all deliberate:

  * Both AI models get the identical bundle. That is what makes "Gemini said 72,
    Groq said 58" a real disagreement rather than two models reading different
    things.
  * Every number carries where it came from — document, page, and the exact line
    of text it was read from. Any claim can be walked back to the page.
  * The bundle is frozen when written and never edited. Months later we can see
    what the system knew AT THE TIME it made a call, not what we know now.

A fact is never silently absent. Anything we could not get is listed in
`missing` with the reason, so a gap reads as a gap and not as a zero.
"""

import json
import os
import re

from . import cashflow, storage, valuation

EVIDENCE = os.path.join(storage.DATA, "evidence")


# ---------------------------------------------------------------- one fact

def fact(value, source, page=None, raw=None, confidence="high", note=None):
    """
    One piece of knowledge, with its paper trail.

    `source` says which document or feed it came from, `page` where in that
    document, and `raw` the actual line of text it was read from. `confidence`
    is "high" for a number read straight off a labelled row, "low" for one
    inferred from a table whose columns ran together when flattened to text.
    """
    body = {"value": value, "source": source, "confidence": confidence}
    if page is not None:
        body["page"] = page
    if raw:
        body["raw"] = raw[:200]
    if note:
        body["note"] = note
    return body


def _value(section, name):
    """The plain value out of a fact, or None."""
    body = (section or {}).get(name)
    return body.get("value") if isinstance(body, dict) else None


# ------------------------------------------------------------ small maths

def _cagr(newest, oldest, years):
    """Compound annual growth, as a percentage. None when it cannot be said."""
    if not newest or not oldest or oldest <= 0 or years <= 0:
        return None
    return round(((newest / oldest) ** (1 / years) - 1) * 100, 2)


def _pct(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole * 100, 2)


def _direction(series):
    """Is this getting better, worse, or neither? Newest value is first."""
    clean = [v for v in (series or []) if v is not None]
    if len(clean) < 2:
        return None
    if clean[0] > clean[-1]:
        return "improving"
    if clean[0] < clean[-1]:
        return "declining"
    return "flat"


PRICE_BAND = re.compile(
    r"(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d+)?)\s*(?:to|-|–|—)\s*(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d+)?)",
    re.I)


def read_price_band(text):
    """'Rs.601 to Rs.632' -> (601.0, 632.0). None when it isn't a band yet."""
    if not text:
        return None, None
    match = PRICE_BAND.search(text)
    if not match:
        return None, None
    try:
        low = float(match.group(1).replace(",", ""))
        high = float(match.group(2).replace(",", ""))
    except ValueError:
        return None, None
    return (low, high) if low <= high else (high, low)


FRESH_AMOUNT = re.compile(
    r"fresh issue.{0,400}?aggregating up to\s*(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*"
    r"(million|crore|lakh)", re.I | re.S)


def read_offer_split(text):
    """
    How much of the money goes to the COMPANY (fresh issue) versus to existing
    owners selling out (offer for sale). This matters: fresh money funds growth,
    an offer for sale is the founders taking cash off the table.

    These figures sit in a table, and a table flattened to plain text runs its
    columns together, so this reading is marked low confidence and keeps the
    line it came from. Better a checkable provisional number than a confident
    wrong one.
    """
    if not text:
        return None
    match = FRESH_AMOUNT.search(text)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    unit = match.group(2).lower()
    millions = {"million": 1, "crore": 10, "lakh": 0.1}[unit]
    return {"fresh_issue_rs_million": round(amount * millions, 2),
            "raw": " ".join(match.group(0).split())[:200]}


# ------------------------------------------------------------- the bundle

def _read_doc(ipo_id, which):
    path = os.path.join(storage.DATA, "docs", ipo_id, f"{which}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _subscription_series(ipo_id):
    """Every subscription reading we have ever taken, oldest first."""
    series = []
    for entry in storage.read_history(ipo_id):
        if entry.get("kind") != "nse_calendar":
            continue
        times = ((entry.get("data") or {}).get("subscription") or {}).get("times")
        if times is None:
            continue
        if series and series[-1]["times"] == times:
            continue                      # unchanged since last reading
        series.append({"at": entry.get("at"), "times": times})
    return series


def build(ipo_id: str) -> dict:
    """Assemble one IPO's evidence bundle from everything on disk."""
    record = storage.read_record(ipo_id) or {}
    abridged = _read_doc(ipo_id, "abridged")
    full = _read_doc(ipo_id, "full")

    bundle = {
        "ipo_id": ipo_id,
        "built_at": storage.now(),
        "name": record.get("name"),
        "symbol": record.get("symbol"),
        "type": record.get("type"),
        "status": record.get("status"),
        "offer": {},
        "financials": {},
        "valuation": {},
        "cash": {},
        "derived": {},
        "demand": {},
        "documents": {},
        "missing": [],
        "conflicts": [],
    }
    missing = bundle["missing"]
    conflicts = bundle["conflicts"]
    nse = "nse_calendar"

    # ---- the offer itself -------------------------------------------------
    offer = bundle["offer"]
    low, high = read_price_band(record.get("price_band"))
    if high:
        offer["price_low"] = fact(low, nse, raw=record.get("price_band"))
        offer["price_high"] = fact(high, nse, raw=record.get("price_band"))
    else:
        missing.append({"what": "price band",
                        "why": "not announced yet" if record.get("status") == "upcoming"
                               else "NSE gave no readable band"})

    if record.get("issue_size_shares"):
        offer["issue_size_shares"] = fact(record["issue_size_shares"], nse)
        if high:
            offer["issue_size_rs_million"] = fact(
                round(record["issue_size_shares"] * high / 1_000_000, 2),
                "computed", note="shares × top of the price band")
    else:
        missing.append({"what": "issue size", "why": "NSE did not report it"})

    for key in ("open", "close"):
        if (record.get("dates") or {}).get(key):
            offer[f"{key}_date"] = fact(record["dates"][key], nse)

    if abridged:
        split = read_offer_split(
            ((abridged.get("sections") or {}).get("offer_details") or {}).get("text"))
        if split:
            offer["fresh_issue_rs_million"] = fact(
                split["fresh_issue_rs_million"], "abridged prospectus",
                page=(abridged["sections"]["offer_details"] or {}).get("page"),
                raw=split["raw"], confidence="low",
                note="read from a table flattened to text — verify before relying on it")
        else:
            missing.append({"what": "fresh issue vs offer for sale split",
                            "why": "could not be read from the offer details table"})

    # Two sources disagreeing is a FINDING, not something to average away.
    # The company cannot raise more in a fresh issue than the whole offer is
    # worth, so if it reads that way, one of the two readings is wrong and the
    # scorer must not lean on either.
    fresh = (offer.get("fresh_issue_rs_million") or {}).get("value")
    total = (offer.get("issue_size_rs_million") or {}).get("value")
    if fresh and total and fresh > total * 1.02:
        conflicts.append({
            "what": "fresh issue is larger than the whole offer",
            "fresh_issue_rs_million": fresh,
            "issue_size_rs_million": total,
            "means": "NSE's share count and the prospectus table do not agree; "
                     "treat the offer split as unknown until checked by hand"})
        offer["fresh_issue_rs_million"]["confidence"] = "contradicted"

    # ---- the financials ---------------------------------------------------
    ratios = (abridged or {}).get("ratios") or {}
    if not abridged:
        missing.append({"what": "three-year financials",
                        "why": "no abridged prospectus on file yet"})
    elif not ratios:
        missing.append({"what": "three-year financials",
                        "why": "abridged prospectus read, but no three-figure rows matched"})

    for name, body in ratios.items():
        bundle["financials"][name] = fact(
            body["years"], "abridged prospectus",
            page=body.get("page"), raw=body.get("raw"))

    # ---- what those financials mean --------------------------------------
    derived = bundle["derived"]
    revenue = (ratios.get("revenue") or {}).get("years")
    pat = (ratios.get("pat") or {}).get("years")
    ebitda = (ratios.get("ebitda") or {}).get("years")

    if revenue and len(revenue) == 3:
        derived["revenue_cagr_2y_pct"] = fact(
            _cagr(revenue[0], revenue[2], 2), "computed",
            note="two years of compounding, newest over oldest")
        derived["revenue_growth_latest_pct"] = fact(
            _pct(revenue[0] - revenue[1], revenue[1]), "computed")
    if pat and len(pat) == 3:
        derived["pat_cagr_2y_pct"] = fact(_cagr(pat[0], pat[2], 2), "computed")
    if revenue and pat and len(revenue) == len(pat):
        derived["pat_margin_pct"] = fact(
            [_pct(p, r) for p, r in zip(pat, revenue)], "computed",
            note="profit as a share of sales, newest first")
    if revenue and ebitda and len(revenue) == len(ebitda):
        derived["ebitda_margin_pct"] = fact(
            [_pct(e, r) for e, r in zip(ebitda, revenue)], "computed")

    for name in ("roe_pct", "roce_pct"):
        years = (ratios.get(name) or {}).get("years")
        if years:
            derived[f"{name}_latest"] = fact(years[0], "abridged prospectus",
                                             page=(ratios[name] or {}).get("page"))
            derived[f"{name}_direction"] = fact(_direction(years), "computed")
        elif abridged:
            missing.append({"what": name.replace("_pct", "").upper(),
                            "why": "this document does not print it"})

    # ---- what the company is charging, in multiples ----------------------
    chapter = ((full or {}).get("sections") or {}).get("basis_for_price")
    if chapter and chapter.get("text"):
        priced = valuation.analyse(chapter["text"], low, high)
        page = chapter.get("start_page")
        eps = priced.get("eps") or {}
        if eps.get("read"):
            bundle["valuation"]["eps_basic_latest"] = fact(
                eps["basic_latest"], "full prospectus", page=page,
                raw=f"fiscal {eps['latest_fiscal']}",
                note=f"checked: our rows reproduce the document's own weighted "
                     f"average of {eps['weighted_average_stated']}")
            bundle["valuation"]["eps_weighted_average"] = fact(
                eps.get("weighted_average_stated"), "full prospectus", page=page)
        if priced.get("vs_benchmark"):
            bundle["valuation"]["pe_at_cap_price"] = fact(
                priced["company_pe_at_cap"], "computed", page=page,
                note="top of the NSE price band divided by the latest basic EPS "
                     "— the document itself leaves this blank, because the band "
                     "was not fixed when it was filed")
            bundle["valuation"]["benchmark_pe"] = fact(
                priced["benchmark_pe"], "full prospectus", page=page,
                confidence="high" if priced["peer_pe_verified_count"] else "low",
                note=priced["benchmark_basis"])
            bundle["valuation"]["vs_benchmark"] = fact(
                priced["vs_benchmark"], "computed", page=page,
                raw=priced["vs_benchmark_plain"])
            bundle["valuation"]["peers_verified"] = fact(
                priced["peer_pe_verified_count"], "full prospectus", page=page,
                note=f"{priced['peer_count']} peer row(s) parsed")
        for note in priced.get("notes", []):
            missing.append({"what": "valuation comparison", "why": note})
        if not priced.get("notes") and not priced.get("vs_benchmark"):
            missing.append({"what": "valuation comparison",
                            "why": "the price chapter gave no usable comparison"})
    elif full:
        missing.append({"what": "valuation comparison",
                        "why": "no basis-for-price chapter found in the prospectus"})

    # ---- does the profit arrive as money? -------------------------------
    if full:
        cash = cashflow.analyse(
            (full.get("sections") or {}),
            pat_years=_value(bundle["financials"], "pat"),
            ebitda_years=_value(bundle["financials"], "ebitda"),
            revenue_years=_value(bundle["financials"], "revenue"))
        if cash.get("read"):
            where = f"full prospectus ({cash['chapter']} chapter)"
            bundle["cash"]["operating_cash_flow"] = fact(
                cash["operating_cash_flow"], where, page=cash.get("page_hint"),
                raw=cash.get("raw"), note="newest year first")
            for name in ("conversion_vs_ebitda", "conversion_vs_pat"):
                if cash.get(name) is not None:
                    bundle["cash"][name] = fact(
                        cash[name], "computed",
                        note="1.0 means the reported profit arrived as cash")
            if cash.get("free_cash_flow"):
                bundle["cash"]["free_cash_flow"] = fact(
                    cash["free_cash_flow"], "computed",
                    note="operating cash flow after paying for equipment")
            bundle["cash"]["negative_in_all_years"] = fact(
                cash["negative_in_all_years"], "computed")
            bundle["cash"]["negative_in_latest_year"] = fact(
                cash["negative_in_latest"], "computed")
            if cash.get("receivables_share_of_sales_growth") is not None:
                bundle["cash"]["receivables_share_of_sales_growth"] = fact(
                    cash["receivables_share_of_sales_growth"], "computed",
                    note="how much of the extra sales is money not yet collected")
            for note in cash.get("notes", []):
                bundle["conflicts"].append({"what": "cash flow warning",
                                            "means": note})
        else:
            missing.append({"what": "cash flow", "why": cash.get("why")})

    # ---- demand -----------------------------------------------------------
    series = _subscription_series(ipo_id)
    if series:
        bundle["demand"]["subscription_times"] = fact(series[-1]["times"], nse,
                                                      note=f"as at {series[-1]['at']}")
        bundle["demand"]["subscription_history"] = fact(series, nse,
                                                        note="every change we recorded")
    else:
        why = ("the issue has not opened yet" if record.get("status") == "upcoming"
               else "NSE reported no subscription figure")
        missing.append({"what": "subscription", "why": why})

    for absent, why in (
            ("grey market premium", "no free source built yet"),
            ("anchor investor allotment", "not collected yet"),
            ("broker conviction", "not collected yet"),
            ("media coverage", "not collected yet"),
            ("retail attention", "not collected yet")):
        missing.append({"what": absent, "why": why})

    # ---- what documents we hold ------------------------------------------
    if abridged:
        bundle["documents"]["abridged"] = {
            "pages": abridged.get("total_pages"),
            "url": abridged.get("url"),
            "fields": sorted((abridged.get("sections") or {}).keys()),
            "read_at": abridged.get("fetched_at"),
        }
    if full:
        bundle["documents"]["full"] = {
            "pages": full.get("total_pages"),
            "url": full.get("url"),
            "chapters": {name: [body.get("start_page"), body.get("end_page")]
                         for name, body in (full.get("sections") or {}).items()},
            "read_at": full.get("fetched_at"),
        }
    else:
        missing.append({"what": "full prospectus",
                        "why": "not downloaded yet — will retry next run"})

    return bundle


# ------------------------------------------------------------ writing it down

def save(bundle: dict) -> str:
    """
    Freeze this bundle. Snapshots are named by the moment they were built and
    are never rewritten — that is what lets us ask later what the system knew
    at the time. `latest.json` is a convenience copy and can be deleted.
    """
    folder = os.path.join(EVIDENCE, bundle["ipo_id"])
    os.makedirs(folder, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", bundle["built_at"])[:14]
    path = os.path.join(folder, f"{stamp}.json")
    for target in (path, os.path.join(folder, "latest.json")):
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(bundle, handle, indent=1, ensure_ascii=False)
    return path


def summarise(bundle: dict) -> str:
    """One line for the run log."""
    have = (len(bundle["financials"]) + len(bundle["derived"])
            + len(bundle["demand"]) + len(bundle["offer"]))
    line = (f"{have} facts, {len(bundle['documents'])} document(s), "
            f"{len(bundle['missing'])} gap(s)")
    if bundle.get("conflicts"):
        line += f", {len(bundle['conflicts'])} CONFLICT(s)"
    return line
