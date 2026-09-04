"""
NSE — the IPO calendar and subscription data.

Verified working 4 Sep 2026 from a GitHub Actions runner, using a Chrome TLS
fingerprint and no proxy. Two endpoints answer; a third (quote-equity) is
blocked on the same session, so prices come from elsewhere.

Note on field names: we do not yet know every field NSE returns. Rather than
guess, this collector keeps the whole raw response and reports which keys it
saw, so the first real run tells us what exists.
"""

from ..dates import to_iso
from ..http import FetchError, get_json, nse_session

REFERER = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"

UPCOMING = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
CURRENT = "https://www.nseindia.com/api/ipo-current-issue"


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def _number(value):
    """NSE sends numbers as text, sometimes with commas, sometimes as '-'."""
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _to_record(raw: dict, status: str) -> dict:
    """
    Turn one NSE row into our shape.

    `raw` is kept whole. Fields we don't understand yet are not lost.
    """
    name = _clean(raw.get("companyName") or raw.get("symbol") or "")

    # NSE writes "08-Sep-2026"; we store "2026-09-08" so dates can be compared.
    open_iso = to_iso(raw.get("issueStartDate"))
    close_iso = to_iso(raw.get("issueEndDate"))

    # Discovered on the first real run: subscription is in this same feed.
    #   noOfTime          how many times the issue is subscribed
    #   noOfsharesBid     shares people have applied for
    #   noOfSharesOffered shares on offer
    offered = _number(raw.get("noOfSharesOffered"))
    bid = _number(raw.get("noOfsharesBid"))
    times = _number(raw.get("noOfTime"))
    if times is None and offered and bid:
        times = round(bid / offered, 4)          # work it out if not given

    return {
        "name": name,
        "symbol": _clean(raw.get("symbol")),
        "type": "sme" if str(raw.get("series", "")).upper() == "SME" else "mainboard",
        "status": status,
        "dates": {
            "open": open_iso,
            "close": close_iso,
            "open_as_given": _clean(raw.get("issueStartDate")),
            "close_as_given": _clean(raw.get("issueEndDate")),
        },
        "price_band_text": _clean(raw.get("issuePrice")),
        "issue_size_shares": _number(raw.get("issueSize")),
        "subscription": {
            "times": times,
            "shares_offered": offered,
            "shares_bid": bid,
        },
        "also_on_bse": str(raw.get("isBse", "")) == "1",
        "raw": raw,
        "source": "nse",
    }


def fetch():
    """
    Return (records, discovered_field_names).

    Raises FetchError if NSE is unreachable — the caller decides what to do,
    so one dead source never stops the whole run.
    """
    session = nse_session()
    records, seen_keys = [], set()

    for url, status, label in [
        (UPCOMING, "upcoming", "NSE upcoming issues"),
        (CURRENT, "open", "NSE current issues"),
    ]:
        rows = get_json(session, url, referer=REFERER, what=label)
        if not isinstance(rows, list):
            raise FetchError(f"{label}: expected a list, got {type(rows).__name__}")
        for row in rows:
            if isinstance(row, dict):
                seen_keys.update(row.keys())
                records.append(_to_record(row, status))

    return records, sorted(seen_keys)
