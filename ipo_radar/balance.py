"""
Reading the balance sheet — what the company owns, owes, and is owed.

The other readers answer "is it growing?" and "does the profit arrive as money?".
This one answers the question that decides whether a bad year is survivable:
how much does it owe, and can it pay what falls due this year?

Three numbers do most of the work:

  **Debt to equity** — borrowings divided by what the owners have in it. Under
  0.5 the company is funded mainly by its owners; above 2 the lenders are in
  charge, and a slow year becomes a crisis rather than a disappointment.

  **Current ratio** — money due in within a year, divided by money due out
  within a year. Below 1.0 the company owes more in the next twelve months than
  it expects to receive, and something has to give.

  **Receivable days** — how long customers take to pay. This is the same story
  the cash flow statement tells, seen from the other side, and it is the honest
  check on a sales figure: revenue that turns into an invoice nobody pays is not
  really revenue yet.

A wrinkle worth knowing: these tables carry a "Note No." column between the
label and the figures, so a row reads

    "(a) Property, Plant and Equipment 3 3,110.09 3,244.37 3,244.80"

— four numbers for three years. We allow one small note number to be skipped
and then take exactly three figures, insisting nothing numeric follows, so we
can never run on into the row beneath.
"""

import re

# A figure as these tables print it. Two traps, both hit in real documents:
#
#   * A bracketed number can be a negative amount — (468.55) — or a FOOTNOTE
#     marker — "Net worth, as restated(5)". So a bracketed figure only counts as
#     a number when it carries a decimal point or a thousands separator, which
#     every real amount in these statements does and no footnote marker does.
#   * A figure must not be cut short. Matching "1,972" out of "1,972.68" gives a
#     number that looks perfectly reasonable and is wrong by a factor of a
#     thousand, so the row must not be followed by a digit, comma or point.
NUMBER = (r"(?:\(-?\d[\d,]*\.\d+\)|\(-?\d{1,3},[\d,]*(?:\.\d+)?\)"
          r"|-?\d[\d,]*(?:\.\d+)?)")
NOTE = r"(?:\d{1,3}\s+)?"          # the Note No. column, when present

# What we look for, in the wordings these documents use. Order matters only
# within a key: the first pattern that yields a clean three-year row wins.
ROWS = {
    # Ratios these documents usually print for themselves. Preferring the
    # printed figure over our own arithmetic is deliberate — it is the company's
    # own definition, audited, and it cannot be thrown off by us picking the
    # wrong row. We still compute our own where the pieces are available, and a
    # disagreement between the two is reported rather than resolved.
    "printed_debt_to_equity": [r"total debt\s*/\s*equity", r"debt[\s-]?equity ratio",
                               r"debt to equity ratio"],
    "printed_current_ratio": [r"current ratio"],

    "total_debt": [r"total debt", r"total borrowings"],
    "net_worth": [r"net worth,? as restated", r"total equity(?!\s+and)", r"net worth"],
    "total_assets": [r"total assets"],
    "current_assets": [r"total current assets"],
    "current_liabilities": [r"total current liabilities"],
    "inventories": [r"inventories"],
    "trade_receivables": [r"trade receivables"],
}

# Plausible ranges. A figure outside these did not come from the row we thought.
SANE = {
    "printed_debt_to_equity": (0, 20),
    "printed_current_ratio": (0, 20),
}

# What may sit between a label and its figures: a footnote marker in brackets,
# a unit ("₹ million", "Times"), a column letter "(C)", and/or a Note number.
# Crucially this may NOT swallow a whole figure — an earlier version did, and
# quietly reported the second, third and fourth columns of a four-column row.
PREFIX = r"(?:\(\d{1,3}\)|\[\d{1,3}\])?[^\d]{0,25}?(?:\d{1,3}\s+)?"

# Words that mean the row we matched is a RATIO table, not an amount. "Total
# Debt Total Equity 0.25 0.27" is a header pair above a debt-to-equity figure,
# not ₹0.25 million of borrowings.
FORBIDDEN = {
    "total_debt": ("equity", "ratio", "times"),
    "net_worth": ("ratio", "times"),
}

MARKERS = re.compile(
    r"total equity|net worth|total debt|total borrowings|current ratio", re.I)


def _num(token):
    token = token.strip()
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return -value if negative else value


def _three(flat, pattern):
    """
    The three yearly figures on the row this pattern names, newest first.

    Up to 30 characters of clutter are allowed between the label and the
    figures, because these tables put all sorts of things there — a Note number,
    a unit ("₹ million", "Times"), a footnote marker, a column letter "(C)".
    We then take exactly three figures and insist nothing numeric follows, so a
    reading can never run on into the row beneath.
    """
    match = re.search(
        rf"{pattern}{PREFIX}({NUMBER}\s+{NUMBER}\s+{NUMBER})(?![\d.,])(?!\s*-?\d[\d,]*\.\d)",
        flat, re.I)
    if not match:
        return None, None
    values = [_num(t) for t in match.group(1).split()]
    if len(values) != 3 or any(v is None for v in values):
        return None, None
    return values, " ".join(match.group(0).split())[:130]


def read_everywhere(sections: dict) -> dict:
    """
    Gather each figure from wherever in the document it actually appears.

    Picking one "balance sheet chapter" turned out to be wrong: the restated
    balance sheet, the KPI table that prints debt-to-equity, and the working
    capital notes sit in different chapters, and which chapter depends on where
    our heading finder happened to draw the lines. So we read every chapter and
    take the first clean reading of each figure, richest chapter first. Each
    figure remembers which chapter it came from.
    """
    ordered = sorted(
        ((name, body) for name, body in (sections or {}).items()),
        key=lambda pair: len(MARKERS.findall((pair[1] or {}).get("text") or "")),
        reverse=True)

    merged = {}
    for name, body in ordered:
        text = (body or {}).get("text") or ""
        if not text:
            continue
        for field, found in read(text).items():
            if field not in merged:
                merged[field] = dict(found, chapter=name,
                                     page=(body or {}).get("start_page"))

        # Borrowings are split into the long-term part and the part due this
        # year, on two rows with the same label. Added together they are what
        # the company actually owes — our fallback when no "total debt" row is
        # printed anywhere.
        if "total_debt" not in merged:
            flat = re.sub(r"\s+", " ", text.replace("\r", " "))
            hits = []
            for match in re.finditer(
                    rf"borrowings{PREFIX}({NUMBER}\s+{NUMBER}\s+{NUMBER})"
                    rf"(?![\d.,])(?!\s*-?\d[\d,]*\.\d)", flat, re.I):
                values = [_num(t) for t in match.group(1).split()]
                if len(values) == 3 and all(v is not None for v in values):
                    hits.append(values)
                if len(hits) == 2:
                    break
            if len(hits) == 2:
                merged["total_debt"] = {
                    "years": [round(a + b, 2) for a, b in zip(*hits)],
                    "raw": "long-term borrowings + borrowings due this year",
                    "chapter": name, "page": (body or {}).get("start_page")}
    return merged


def read(text: str) -> dict:
    flat = re.sub(r"\s+", " ", (text or "").replace("\r", " "))
    found = {}
    for name, patterns in ROWS.items():
        for pattern in patterns:
            values, raw = _three(flat, pattern)
            if not values:
                continue
            match_text = raw
            banned = FORBIDDEN.get(name, ())
            if banned and any(word in match_text.lower() for word in banned):
                continue
            low, high = SANE.get(name, (None, None))
            if low is not None and not all(low <= v <= high for v in values):
                continue
            found[name] = {"years": values, "raw": raw, "matched": pattern}
            break
    return found


def analyse(sections: dict, revenue_years=None) -> dict:
    rows = read_everywhere(sections)
    if not rows:
        return {"read": False, "why": "no balance sheet figures found in the "
                                      "chapters we hold"}
    chapter = ", ".join(sorted({body["chapter"] for body in rows.values()}))
    page = next(iter(rows.values())).get("page")

    result = {"read": False, "chapter": chapter, "page_hint": page,
              "rows_found": sorted(rows), "notes": []}
    for name, body in rows.items():
        result[name] = body["years"]
        result[f"raw_{name}"] = body["raw"]

    # --- how much is owed, against what the owners have in it ---------------
    printed = (rows.get("printed_debt_to_equity") or {}).get("years")
    debt = (rows.get("total_debt") or {}).get("years")
    worth = (rows.get("net_worth") or {}).get("years")

    computed = None
    if debt and worth and all(w for w in worth):
        computed = [round(d / w, 3) for d, w in zip(debt, worth)]
        if not all(0 <= v <= 20 for v in computed):
            computed = None

    # One year wildly out of step with the other two means a column was read
    # from the wrong place. Refuse the whole reading rather than publish it.
    # A ratio that swings more than fiftyfold across three years is a misread,
    # not a business. Below that, a big swing is real news — a company that
    # cleared its debt — so it is kept and pointed out rather than thrown away.
    if computed and min(computed) > 0 and max(computed) > min(computed) * 50:
        result["notes"].append(
            f"our own debt-to-equity reading {computed} swings too far to be "
            f"credible — discarded")
        computed = None
    elif computed and min(computed) > 0 and max(computed) > min(computed) * 5:
        result["notes"].append(
            f"borrowings changed sharply over the three years — "
            f"debt-to-equity went {computed[-1]} to {computed[0]}")

    if printed:
        result["debt_to_equity"] = printed
        result["debt_to_equity_source"] = "printed in the document"
        if computed and printed[0] and abs(computed[0] - printed[0]) / printed[0] > 0.15:
            result["notes"].append(
                f"our own sum gives a debt-to-equity of {computed[0]} where the "
                f"document prints {printed[0]} — using the document's figure")
    elif computed:
        result["debt_to_equity"] = computed
        result["debt_to_equity_source"] = "computed from total debt ÷ net worth"

    # --- can it pay what falls due this year? ------------------------------
    printed_ratio = (rows.get("printed_current_ratio") or {}).get("years")
    assets = (rows.get("current_assets") or {}).get("years")
    liabilities = (rows.get("current_liabilities") or {}).get("years")
    if printed_ratio:
        result["current_ratio"] = printed_ratio
        result["current_ratio_source"] = "printed in the document"
    elif assets and liabilities and all(liabilities):
        ratio = [round(a / l, 2) for a, l in zip(assets, liabilities)]
        if all(0 <= v <= 20 for v in ratio):
            result["current_ratio"] = ratio
            result["current_ratio_source"] = "computed from current assets ÷ liabilities"

    # --- how long customers take to pay ------------------------------------
    receivables = (rows.get("trade_receivables") or {}).get("years")
    if receivables and revenue_years and all(revenue_years):
        days = [round(r / s * 365, 1) for r, s in zip(receivables, revenue_years)]
        # A row read from the wrong column shows up as one year wildly out of
        # step with the others, so we refuse the whole reading rather than
        # publish a plausible-looking average of a mistake.
        steady = max(days) <= min(days) * 8 if min(days) > 0 else False
        if all(1 <= d <= 730 for d in days) and steady:
            result["receivable_days"] = days
            if days[0] > days[-1] * 1.3:
                result["notes"].append(
                    f"customers are taking {days[0]:.0f} days to pay, against "
                    f"{days[-1]:.0f} two years ago — the money is going out further")

    result["read"] = bool(result.get("debt_to_equity") or result.get("current_ratio"))
    if not result["read"]:
        result["why"] = ("found the chapter but could not read a debt or "
                         "liquidity figure from it")
    return result
