"""
Turning NSE's dates into dates the computer can actually compare.

NSE writes dates as "08-Sep-2026". That reads fine to a person but to a
computer it is just letters, and letters sort alphabetically:

    "10-Sep-2026" comes BEFORE "4-Sep-2026"

which would quietly break every "is this open today", every sort, and every
"how many days until listing". So every date is converted, once, on the way
in, to the international format 2026-09-08 — which sorts correctly by simple
comparison and is unambiguous everywhere.
"""

from datetime import date, datetime

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Formats we have actually seen, plus the obvious near-misses.
KNOWN_FORMATS = ["%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]


def to_iso(text):
    """
    '08-Sep-2026'  ->  '2026-09-08'

    Returns None if the text cannot be understood, rather than guessing.
    A missing date is recorded as missing; an invented one would be worse.
    """
    if not text:
        return None
    text = str(text).strip()
    if not text or text in ("-", "NA", "N/A"):
        return None

    for fmt in KNOWN_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    # Last resort: pull the pieces apart ourselves.
    parts = text.replace("/", "-").split("-")
    if len(parts) == 3:
        day, month, year = parts
        month_num = MONTHS.get(month[:3].lower())
        if month_num and day.isdigit() and year.isdigit():
            try:
                return date(int(year), month_num, int(day)).isoformat()
            except ValueError:
                pass
    return None


def month_key(iso_date):
    """'2026-09-08' -> '2026-09'. Used to build a readable, stable id."""
    return iso_date[:7] if iso_date and len(iso_date) >= 7 else "undated"
