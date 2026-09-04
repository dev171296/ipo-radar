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


def summarise(found: dict) -> str:
    if not found:
        return "no fields read"
    return " ".join(f"{name}(p{body['page']})" for name, body in sorted(found.items()))
