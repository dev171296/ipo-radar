"""
Reading the abridged prospectus.

This document is not a shortened version of the big one — it is a form whose
layout SEBI mandates. Every company fills in the same labelled boxes, which is
why we can read it by looking for the labels rather than searching the text.

What the form gives us, all of it useful:

  the promoters, by name
  the split between money going to the company (fresh issue) and money going
      to existing shareholders (offer for sale)
  the shares reserved for institutions, wealthy individuals and retail
  what the selling shareholders originally paid per share — so you can see what
      insiders paid against what you are being asked to pay
  a summary of the accounts
  the reasons the money is being raised

Anything the form does not carry — full litigation, related-party dealings,
every risk factor — comes from the 500-page document instead.
"""

import re

# Each field: the label as it appears on the form, and what we call it.
# Several wordings per field because companies punctuate them differently.
FIELDS = {
    "cin": ["corporate identity number"],
    "promoters": ["our promoters", "the promoters of our company",
                  "our promoter"],
    "offer_details": ["details of the offer to public", "details of the offer"],
    "reservation": ["eligibility and share reservation"],
    "selling_shareholders": ["details of the offer for sale by the selling"],
    "objects": ["objects of the offer", "objects of the issue"],
    "risk_factors": ["risk factors"],
    "financials": ["financial information", "summary of financial",
                   "restated financial"],
    "price_band": ["price band", "floor price"],
    "lead_managers": ["book running lead manager", "lead manager"],
    "registrar": ["registrar to the offer", "registrar to the issue"],
}

# How much text to keep after a label before we assume the box has ended.
CAPTURE_CHARS = 2500


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def read(pages: list) -> dict:
    """
    Pull the labelled boxes out of the form.

    Returns {field: {page, label_seen, text}}. Fields the form does not carry
    are simply absent — we report what is missing rather than inventing it.
    """
    whole = "\n".join(pages)
    flat = _flat(whole)
    found = {}

    # Which page does each character position fall on? Lets us cite a page.
    boundaries, running = [], 0
    for page_number, text in enumerate(pages, start=1):
        running += len(_flat(text)) + 1
        boundaries.append((running, page_number))

    def page_at(position):
        for limit, page_number in boundaries:
            if position < limit:
                return page_number
        return len(pages)

    for field, labels in FIELDS.items():
        for label in labels:
            position = flat.find(label)
            if position == -1:
                continue
            body = flat[position:position + CAPTURE_CHARS]
            found[field] = {
                "page": page_at(position),
                "label_seen": label,
                "chars": len(body),
                "text": body,
            }
            break                      # first wording that matches wins

    return found


# Ratios we have SEEN in these documents. Each entry is the wording that
# starts the line in the financial table. We match a LINE, not a blob of text,
# because the same words also appear in prose ("revenue from operations grew
# by 12% to ..."), and a blob match happily walks onto the next line and picks
# up numbers that belong to a different row.
RATIO_LABELS = {
    "ebitda": r"ebitda",
    "roe_pct": r"ro(?:e|nw)",
    "roce_pct": r"roce",
    "revenue": r"(?:total\s+)?revenue\s+from\s+operations",
    "pat": r"(?:profit|loss)\s+after\s+tax|profit\s+for\s+the\s+(?:year|period)",
}

# A number as these tables print it: 4,569.71 / 1051.98 / (12.34) for negative.
NUMBER = r"\(?-?\d[\d,]*(?:\.\d+)?\)?%?"

# Noise that sits between the label and the first figure: footnote markers,
# unit notes, and the odd stray bracket.
_TRAILING_NOTE = re.compile(
    r"^\s*(?:\(\d+\)|\*+|\#+|\(?(?:rs|inr|₹)[^)]{0,30}\)?|in\s+lakhs?|in\s+millions?"
    r"|in\s+crores?|\(?%\)?|:|-|–)\s*", re.I)


def _num(text):
    text = str(text).strip().rstrip("%")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return -value if negative else value


def _numbers_on(rest: str) -> list:
    """The figures on one table row, after the label has been stripped off."""
    previous = None
    while rest != previous:
        previous = rest
        rest = _TRAILING_NOTE.sub("", rest)
    values = [_num(token) for token in re.findall(NUMBER, rest)]
    return [v for v in values if v is not None]


def read_ratios(pages: list, prefer_page: int = None) -> dict:
    """
    Pull the headline financial figures out as numbers.

    These documents print three years side by side, newest first:
        ROE %   42.12   30.92   10.49
    which is exactly what the growth and profitability factors need. Reported
    as [newest, middle, oldest] with the raw line kept so the reading can be
    checked against the document.

    Two rules keep us honest:
      * we only accept a line that carries EXACTLY three figures — a table row
        with three years and nothing else. A sentence, a two-year table or a
        row with a footnote column is left alone rather than guessed at.
      * if we know which page holds the financial table (prefer_page), we read
        that page first and only widen the search if it yields nothing.
    """
    def scan(page_numbers):
        out = {}
        for page_number in page_numbers:
            for line in pages[page_number - 1].splitlines():
                line = " ".join(line.split())
                if not line:
                    continue
                lowered = line.lower()
                for name, label in RATIO_LABELS.items():
                    if name in out:
                        continue
                    match = re.match(rf"^{label}\b", lowered)
                    if not match:
                        continue
                    values = _numbers_on(line[match.end():])
                    if len(values) == 3:
                        out[name] = {"years": values, "page": page_number,
                                     "raw": line[:100]}
        return out

    all_pages = range(1, len(pages) + 1)
    if prefer_page and 1 <= prefer_page <= len(pages):
        nearby = [n for n in (prefer_page, prefer_page + 1, prefer_page - 1)
                  if 1 <= n <= len(pages)]
        found = scan(nearby)
        if found:
            rest = [n for n in all_pages if n not in nearby]
            for name, body in scan(rest).items():
                found.setdefault(name, body)
            return found
    return scan(all_pages)


def summarise(found: dict) -> str:
    if not found:
        return "no fields read"
    return " ".join(f"{name}(p{body['page']})" for name, body in sorted(found.items()))
