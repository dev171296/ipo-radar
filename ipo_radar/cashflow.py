"""
Reading the cash flow statement — the check on everything else.

Why this matters more than the profit figure
--------------------------------------------
Profit is an opinion; cash is a fact. A company books a sale the moment it ships
the goods, whether or not the customer ever pays. So profit can rise for years
while no money arrives, and the gap shows up here — as receivables growing, as
inventory piling up, as operating cash flow lagging far behind reported profit.

Every one of the classic Indian IPO accidents looks fine on the profit line and
wrong on this page. That is why one of the six veto rules is "negative operating
cash flow in all three years", and why the ratio of cash to profit is worth more
than either number alone.

Where it lives
--------------
Not always in a chapter of its own. Our heading finder locates "Our Promoters"
and "Our Business", and the restated financial statements often sit INSIDE one
of those page ranges rather than starting a section we recognise. So this reader
searches every section we hold for the statement itself, rather than trusting
the filing to be tidy. Measured 7 Sep 2026: the statement sat under `promoters`
for two companies, `business` for one, and `financials` for one.
"""

import re

NUMBER = r"\(?-?[\d,]+(?:\.\d+)?\)?"

# The row we most need, in the wordings these documents actually use.
ROWS = {
    "operating": [
        r"net cash[^\n]{0,50}?operating activities",
        r"net cash (?:in)?flows? (?:from|used in) operating activities",
    ],
    "investing": [
        r"net cash[^\n]{0,50}?investing activities",
    ],
    "financing": [
        r"net cash[^\n]{0,50}?financing activities",
    ],
    "cash_from_operations": [
        r"cash generated from ?/? ?\(?used in\)? ?operations",
        r"cash generated from operations",
        r"cash generated from operation",
    ],
    "profit_before_tax": [
        r"net profit before tax",
        r"profit(?:/\(loss\))? before tax",
    ],
    "capex": [
        r"purchase of property,? plant and equipment",
        r"payments? for acquisition of propert",
        r"purchase of property plant & equipment",
    ],
    "receivables_change": [
        r"\(?increase\)?/?\s*\(?decrease\)?[^\n]{0,40}trade receivables",
    ],
}


def _num(token):
    """A figure as these statements print it: 1,234.56 or (816.80) for negative."""
    token = token.strip()
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return -value if negative else value


def find_statement(sections: dict):
    """
    The pages holding the cash flow statement, whichever chapter they fell into.

    Returns (text, chapter_name, page_hint) or (None, None, None).
    """
    best = (None, None, None, 0)
    for name, body in (sections or {}).items():
        text = (body or {}).get("text") or ""
        score = len(re.findall(r"cash flows? from (?:operating|investing|financing) activities",
                               text, re.I))
        if score > best[3]:
            best = (text, name, (body or {}).get("start_page"), score)
    return best[0], best[1], best[2]


def read(text: str) -> dict:
    """
    Pull the three-year figures out.

    Not line by line, unlike the other readers — and for a reason. In two of the
    four documents this table survives the PDF as a proper row; in the other two
    it comes out as flowing text, the whole table on one wrapped line:

        "...in the years indicated: Particulars Fiscal 2026 2025 2024 (₹ million)
         Net cash flow generated from / (used in) operating activities 258.42
         793.82 (163.18) Net cash generated from / (used in) investing..."

    So we look for the LABEL anywhere, take the three figures that follow it, and
    insist the fourth thing after them is not a figure. That last condition is
    what stops us running on into the next row and mixing two rows together —
    the same mistake that produced the wrong revenue numbers earlier in this
    project.
    """
    if not text:
        return {}

    flat = re.sub(r"\s+", " ", text.replace("\r", " "))
    found = {}
    for name, patterns in ROWS.items():
        for pattern in patterns:
            match = re.search(
                rf"{pattern}[^\d]{{0,25}}?({NUMBER}\s+{NUMBER}\s+{NUMBER})"
                rf"(?!\s*{NUMBER})",
                flat, re.I)
            if not match:
                continue
            values = [_num(t) for t in match.group(1).split()]
            if len(values) == 3 and all(v is not None for v in values):
                found[name] = {"years": values,
                               "raw": " ".join(match.group(0).split())[:130]}
                break
    return found


def analyse(sections: dict, pat_years=None, ebitda_years=None,
            revenue_years=None) -> dict:
    """
    What the cash flow says about the profit.

    The headline is **cash conversion**: operating cash flow divided by EBITDA.
    Around 1.0 means the reported profit is arriving as money. Well below it
    means the profit exists on paper and the cash is sitting in someone else's
    bank account — usually customers who have not paid.
    """
    text, chapter, page = find_statement(sections)
    if not text:
        return {"read": False, "why": "no cash flow statement found in the "
                                      "chapters we hold"}

    rows = read(text)
    operating = (rows.get("operating") or {}).get("years")
    if not operating:
        return {"read": False, "chapter": chapter,
                "why": "found the statement but could not read the operating "
                       "cash flow row"}

    result = {
        "read": True,
        "chapter": chapter,
        "page_hint": page,
        "operating_cash_flow": operating,
        "raw": (rows.get("operating") or {}).get("raw"),
        "rows_found": sorted(rows),
        "notes": [],
    }
    for name in ("investing", "financing", "capex", "cash_from_operations",
                 "receivables_change", "profit_before_tax"):
        if name in rows:
            result[name] = rows[name]["years"]

    latest = operating[0]

    # Cash conversion — against EBITDA if we have it, else against profit.
    if ebitda_years and ebitda_years[0]:
        result["conversion_vs_ebitda"] = round(latest / ebitda_years[0], 3)
        result["conversion_basis"] = "operating cash flow ÷ EBITDA"
    if pat_years and pat_years[0]:
        result["conversion_vs_pat"] = round(latest / pat_years[0], 3)
        result.setdefault("conversion_basis", "operating cash flow ÷ profit after tax")

    result["negative_in_all_years"] = all(v < 0 for v in operating)
    result["negative_in_latest"] = latest < 0

    # Free cash flow: what is left after paying for the equipment that keeps the
    # business running. A company can show positive operating cash and still
    # consume money every year.
    capex = result.get("capex")
    if capex:
        result["free_cash_flow"] = [round(o + c, 2)
                                    for o, c in zip(operating, capex)]

    # The receivables veto, testable straight from this statement: money owed by
    # customers growing faster than the sales that created it.
    receivables = result.get("receivables_change")
    if receivables and revenue_years and len(revenue_years) >= 2:
        rise_in_receivables = -receivables[0]      # printed as negative when up
        rise_in_sales = revenue_years[0] - revenue_years[1]
        if rise_in_receivables > 0 and rise_in_sales > 0:
            share = rise_in_receivables / rise_in_sales
            result["receivables_share_of_sales_growth"] = round(share, 3)
            if share > 0.5:
                result["notes"].append(
                    f"{share * 100:.0f}% of the increase in sales is money not "
                    f"yet collected — receivables rose {rise_in_receivables:,.0f} "
                    f"while sales rose {rise_in_sales:,.0f}")

    return result
