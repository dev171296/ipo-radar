"""
Working out whether two records describe the same IPO.

The problem, measured: roughly a fifth to a third of Indian IPOs are spelled
differently by different sources.

    NSE      "Kanohar Electricals Limited"
    a broker "Kanohar Electricals Ltd"
    a paper  "Kanohar Electricals"

If we match on exact text we end up with three half-filled records for one
company, each missing what the others have. So we normalise the name, and we
keep every spelling we have ever seen in an `aliases` list.

The id itself is fixed the first time we see an IPO and NEVER recomputed —
recomputing it later is how duplicates appear mid-window.
"""

import re

# Words that carry no identity — they differ between sources for the same company.
NOISE = [
    "limited", "ltd", "private", "pvt", "public", "company", "co",
    "corporation", "corp", "india", "incorporated", "inc",
]


def normalise(name: str) -> str:
    """Reduce a company name to its identifying core."""
    text = (name or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)      # drop punctuation
    words = [w for w in text.split() if w and w not in NOISE]
    return " ".join(words)


def slug(name: str) -> str:
    return normalise(name).replace(" ", "-")


def make_id(name: str, open_date: str) -> str:
    """
    A stable id, e.g. '2026-09-kanohar-electricals'.

    Built once at discovery from the name and the opening month, then frozen.
    """
    month = (open_date or "unknown")[:7] if "-" in (open_date or "") else "undated"
    return f"{month}-{slug(name)}"


def same_ipo(a: dict, b: dict) -> bool:
    """
    Is this incoming record the same IPO as one we already hold?

    Three signals, not one: the normalised name must match, and the dates or
    the issue size should agree. Name alone is too weak; dates alone far too weak.
    """
    if normalise(a.get("name", "")) != normalise(b.get("name", "")):
        return False
    a_open = (a.get("dates") or {}).get("open")
    b_open = (b.get("dates") or {}).get("open")
    if a_open and b_open and a_open != b_open:
        return False
    return True


def find_match(existing: list, incoming: dict):
    """Return the existing record this belongs to, or None if it is new."""
    for record in existing:
        if same_ipo(record, incoming):
            return record
    return None
