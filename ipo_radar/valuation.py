"""
Reading the price argument out of the prospectus.

Every RHP contains a chapter called BASIS FOR OFFER PRICE — the company's own
written case for why its shares are worth what it is asking. It carries three
things we can use as numbers rather than prose:

  1. Earnings per share, three years plus a weighted average.
  2. The industry peer group P/E — highest, lowest, average.
  3. Where the layout allows, a row per listed competitor.

The company's OWN P/E is printed as `[●]` in every one of these documents — a
placeholder, because the price band was not fixed when the document was filed.
That is the point of this module: we know the band from NSE and the earnings
from this table, so we can work out the multiple actually being asked and set it
against the peer group. The document cannot do that. We can.

How we know the reading is right
--------------------------------
These tables print a weighted average EPS (the latest year weighted 3, the one
before 2, the one before that 1). So we recompute it from the rows we read. If
our arithmetic reproduces the printed figure, the rows were read correctly —
proof, not hope. If it does not, we say the reading failed rather than pass a
plausible-looking number downstream.

Every wording below was taken from a real document, not imagined:
    "Fiscal 2026 17.43 17.43 3"                       (Kanohar)
    "2026 8.18 8.18 3"                                (Pranav)
    "Financial Year ended March 31, 2026 14.33 14.33 3"  (Prasol)
    "March 31, 2026 9.90 - 3"                         (Glass Wall, two share classes)
"""

import re
import statistics

NUMBER = r"[\d,]+(?:\.\d+)?"
FIGURE = rf"(?:{NUMBER}|-|–|N\.?A\.?)"


def _num(text):
    text = str(text).replace(",", "").strip()
    if text in {"-", "–", "NA", "N.A.", "N.A", ""}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _flat(text):
    """Collapse the line breaks a PDF sprinkles through a table row."""
    return re.sub(r"\s+", " ", (text or "").replace("\r", " "))


# ------------------------------------------------------------ earnings

# A year, two figures, and a weight of 1, 2 or 3. Whatever words introduce the
# year — "Fiscal", "Financial Year ended March 31," or nothing at all — are
# ignored, because the four documents we have use four different phrasings.
EPS_ROW = re.compile(rf"\b(20\d\d)\s+({FIGURE})\s+({FIGURE})\s+([123])\b")

WEIGHTED = re.compile(
    rf"weighted average[^\d\n]{{0,60}}({NUMBER})", re.I)


def read_eps(text: str) -> dict:
    """
    Earnings per share by year, checked against the document's own arithmetic.

    We keep BOTH the latest year and the weighted average. The weighting leans
    on the most recent year by design, which flatters a company whose profit has
    just jumped — so the scorer is given both and decides which to lean on.
    """
    flat = _flat(text)
    years = {}
    for match in EPS_ROW.finditer(flat):
        year, basic, diluted, weight = match.groups()
        year, weight = int(year), int(weight)
        if year in years:
            continue                       # the first table wins
        years[year] = {"basic": _num(basic), "diluted": _num(diluted),
                       "weight": weight}

    usable = {y: b for y, b in years.items() if b["basic"] is not None}
    if len(usable) < 2:
        return {"read": False, "why": "no earnings-per-share rows found"}

    stated_match = WEIGHTED.search(flat)
    stated = _num(stated_match.group(1)) if stated_match else None

    total_weight = sum(b["weight"] for b in usable.values())
    computed = (sum(b["basic"] * b["weight"] for b in usable.values())
                / total_weight) if total_weight else None

    # The proof. If our rows reproduce the printed weighted average, we read the
    # table correctly. If they do not, something is misaligned and we say so.
    checks_out = bool(stated and computed
                      and abs(computed - stated) / stated < 0.02)

    latest = max(usable)
    return {
        "read": checks_out,
        "by_year": years,
        "latest_fiscal": latest,
        "basic_latest": usable[latest]["basic"],
        "diluted_latest": usable[latest]["diluted"],
        "weighted_average_stated": stated,
        "weighted_average_recomputed": round(computed, 2) if computed else None,
        "why": None if checks_out else
               (f"our rows give a weighted average of "
                f"{computed:.2f} but the document prints {stated} — "
                f"the table was not read correctly"
                if computed and stated else
                "the document prints no weighted average to check against"),
    }


# ------------------------------------------------- the industry P/E band

# Highest / Lowest / Average, with anything in between — one document names the
# peer company beside each figure, another puts a note in the middle.
PE_BAND = re.compile(
    rf"highest\s+({NUMBER})\D{{0,80}}?lowest\s+({NUMBER})\D{{0,80}}?average\s+({NUMBER})",
    re.I | re.S)


def read_industry_pe(text: str) -> dict:
    """The highest / lowest / average P/E the company quotes for its industry."""
    match = PE_BAND.search(_flat(text))
    if not match:
        return {}
    return {"highest": _num(match.group(1)),
            "lowest": _num(match.group(2)),
            "average_as_stated": _num(match.group(3)),
            "raw": _flat(match.group(0))[:140]}


# ------------------------------------------------------- the peer table

# Best effort only. Column order differs from document to document, so a row is
# accepted ONLY if price ÷ EPS reproduces the printed P/E. An unverified row is
# reported and never scored — a misaligned column is exactly the sort of error
# that would otherwise slip quietly into a number.
PEER_ROW = re.compile(
    rf"([A-Z][A-Za-z&.,'()\-\s]{{3,70}}?(?:Limited|Ltd\.?))\s+"
    rf"((?:{NUMBER}\s+){{5,12}})", re.S)

COLUMNS = ["revenue", "face_value", "closing_price", "pe", "pb",
           "eps_basic", "eps_diluted", "ronw_pct", "market_cap", "nav"]


def read_peers(text: str) -> list:
    flat = _flat(text)
    start = flat.lower().find("listed peers")
    if start < 0:
        return []

    peers = []
    for match in PEER_ROW.finditer(flat[start:start + 4000]):
        name = " ".join(match.group(1).split())
        name = re.sub(r"^listed peers\s*", "", name, flags=re.I)
        row = {"name": name}
        for column, value in zip(COLUMNS, [_num(f) for f in match.group(2).split()]):
            row[column] = value

        pe, price, eps = row.get("pe"), row.get("closing_price"), row.get("eps_basic")
        row["verified"] = bool(pe and price and eps
                               and abs(price / eps - pe) / pe < 0.05)
        peers.append(row)
    return peers


# ----------------------------------------------------- putting it together

def analyse(chapter_text: str, price_low=None, price_high=None) -> dict:
    """
    What multiple is being asked, and how does it compare?

    Where a peer table parses and verifies, we use the MEDIAN of those P/Es —
    one 159× peer drags an average upward and would make almost any issue look
    cheap. Where it does not parse, we fall back on the average the document
    itself states, and say which basis was used, because they are not equally
    trustworthy.
    """
    eps = read_eps(chapter_text)
    industry = read_industry_pe(chapter_text)
    peers = read_peers(chapter_text)

    verified = [p["pe"] for p in peers if p.get("pe") and p.get("verified")]
    result = {
        "eps": eps,
        "industry_pe_as_stated": industry,
        "peers": peers,
        "peer_count": len(peers),
        "peer_pe_verified_count": len(verified),
        "peer_pe_median": round(statistics.median(verified), 2) if verified else None,
        "notes": [],
    }

    if not eps.get("read"):
        result["notes"].append(eps.get("why") or "earnings per share not read")
        return result
    if not price_high:
        result["notes"].append("no price band yet, so no multiple can be worked out")
        return result

    basic = eps["basic_latest"]
    result["company_pe_at_cap"] = round(price_high / basic, 2)
    if price_low:
        result["company_pe_at_floor"] = round(price_low / basic, 2)
    if eps.get("weighted_average_stated"):
        result["company_pe_on_weighted_eps"] = round(
            price_high / eps["weighted_average_stated"], 2)

    # Which yardstick, and how much to trust it.
    if result["peer_pe_median"]:
        benchmark, basis = result["peer_pe_median"], "median of the verified peer table"
    elif industry.get("average_as_stated"):
        benchmark, basis = industry["average_as_stated"], "the average stated in the document"
    else:
        result["notes"].append("no peer comparison available in this chapter")
        return result

    ratio = result["company_pe_at_cap"] / benchmark
    result["benchmark_pe"] = benchmark
    result["benchmark_basis"] = basis
    result["vs_benchmark"] = round(ratio, 3)
    result["vs_benchmark_plain"] = (
        f"asking {result['company_pe_at_cap']}× earnings against {benchmark}× "
        f"({basis}) — "
        + (f"a {(1 - ratio) * 100:.0f}% discount" if ratio < 1
           else f"a {(ratio - 1) * 100:.0f}% premium"))

    if industry.get("highest") == industry.get("lowest"):
        result["notes"].append(
            "the highest and lowest industry P/E are the same figure — the "
            "company compares itself to a single peer, so this is one opinion, "
            "not a market")

    stated = industry.get("average_as_stated")
    if stated and result["peer_pe_median"]:
        gap = abs(stated - result["peer_pe_median"]) / result["peer_pe_median"]
        if gap > 0.15:
            result["notes"].append(
                f"the document's quoted average P/E ({stated}) sits "
                f"{gap * 100:.0f}% away from the median of its own peer table "
                f"({result['peer_pe_median']}) — an average pulled by an extreme peer")

    # A large discount or premium is a FINDING, not an error — that is the whole
    # point of working the multiple out. Only an impossible multiple means we
    # misread something.
    if not 2 <= result["company_pe_at_cap"] <= 400:
        result["notes"].append(
            f"a multiple of {result['company_pe_at_cap']}× is not credible — "
            f"the earnings figure was probably misread; ignore this comparison")
        result["vs_benchmark"] = None

    return result
