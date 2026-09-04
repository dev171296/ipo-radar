"""
NSE daily price files ("bhavcopy").

Every trading day NSE publishes a file listing every stock's open, high, low,
close and volume. Two files matter to us:

  mainboard : sec_bhavdata_full_DDMMYYYY.csv   (~395 KB, plain CSV)
  SME       : sme DDMMYYYY .csv                (~80 KB, plain CSV)

Both verified working from a GitHub runner on 4 Sep 2026.

Why this beats asking for prices one company at a time:
  - two downloads a day cover every stock we will ever track
  - no per-company requests, so no rate limits to trip over
  - it is NSE's own published data, not scraped from a page
  - the mainboard file also carries DELIVERY quantity, which tells us how much
    was genuinely bought to keep rather than traded in and out that day

The exact column names are read from the file's own header rather than assumed,
because NSE has changed them before.
"""

import csv
import io
from datetime import date, datetime, timedelta, timezone

from ..http import TIMEOUT, FetchError, chrome_session

MAINBOARD = ("https://nsearchives.nseindia.com/products/content/"
             "sec_bhavdata_full_{ddmmyyyy}.csv")
SME = ("https://nsearchives.nseindia.com/archives/sme/bhavcopy/"
       "sme{ddmmyyyy}.csv")

# The same idea is spelled differently in the two files, so we accept any of these.
COLUMN_ALIASES = {
    "symbol": ["symbol"],
    "series": ["series"],
    "open": ["open_price", "open"],
    "high": ["high_price", "high"],
    "low": ["low_price", "low"],
    "close": ["close_price", "close", "last_price"],
    "volume": ["ttl_trd_qnty", "net_trdqty", "traded_qty", "volume"],
    "delivery_qty": ["deliv_qty"],
    "delivery_pct": ["deliv_per"],
}

_cache = {}          # one download per file per run


def _tidy(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_")


def _pick(row: dict, field: str):
    """Find a value by any of its known column names."""
    for alias in COLUMN_ALIASES[field]:
        if alias in row and str(row[alias]).strip() not in ("", "-"):
            return str(row[alias]).strip()
    return None


def _number(text):
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse(text: str, segment: str):
    """Turn one CSV file into {SYMBOL: price bar}."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise FetchError(f"{segment} file had no header row")

    reader.fieldnames = [_tidy(n) for n in reader.fieldnames]
    bars, columns = {}, reader.fieldnames

    for raw in reader:
        row = {_tidy(k): v for k, v in raw.items() if k}
        symbol = _pick(row, "symbol")
        if not symbol:
            continue
        close = _number(_pick(row, "close"))
        if close is None or close <= 0:
            continue                      # skip rows with no usable price
        bars[symbol.upper()] = {
            "symbol": symbol.upper(),
            "series": _pick(row, "series"),
            "open": _number(_pick(row, "open")),
            "high": _number(_pick(row, "high")),
            "low": _number(_pick(row, "low")),
            "close": close,
            "volume": _number(_pick(row, "volume")),
            "delivery_qty": _number(_pick(row, "delivery_qty")),
            "delivery_pct": _number(_pick(row, "delivery_pct")),
            "segment": segment,
            "source": f"nse_bhavcopy_{segment}",
        }
    return bars, columns


def _download(url: str, segment: str):
    session = chrome_session()
    session.headers.update({"Referer": "https://www.nseindia.com/"})
    resp = session.get(url, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise FetchError(f"{segment} bhavcopy: HTTP {resp.status_code}")
    if len(resp.content) < 5000:
        # A short reply is an error page wearing a success code.
        raise FetchError(f"{segment} bhavcopy: only {len(resp.content)} bytes")
    return _parse(resp.text, segment)


def for_date(day: date):
    """
    Prices for one day: {SYMBOL: bar}, covering mainboard and SME together.

    Raises FetchError only if BOTH files are missing.
    """
    key = day.isoformat()
    if key in _cache:
        return _cache[key]

    stamp = day.strftime("%d%m%Y")
    combined, notes = {}, []

    for segment, url in [("mainboard", MAINBOARD.format(ddmmyyyy=stamp)),
                         ("sme", SME.format(ddmmyyyy=stamp))]:
        try:
            bars, columns = _download(url, segment)
            combined.update(bars)
            notes.append(f"{segment}: {len(bars)} stocks")
            print(f"      {segment} columns: {', '.join(columns)}")
        except Exception as exc:
            notes.append(f"{segment}: {type(exc).__name__} {str(exc)[:70]}")

    if not combined:
        raise FetchError(f"no bhavcopy for {key} — {'; '.join(notes)}")

    print(f"      {key}: {'; '.join(notes)}")
    _cache[key] = combined
    return combined


IST = timezone(timedelta(hours=5, minutes=30))
PUBLISHED_AFTER_HOUR = 19        # the day's file is reliably up by 7pm IST


def india_now():
    return datetime.now(IST)


def todays_file_should_exist() -> bool:
    """
    Has NSE had time to publish today's file?

    The market closes at 3:30pm IST and the file follows. Before 7pm IST we do
    not expect it, so its absence is normal rather than a problem.
    """
    now = india_now()
    if now.weekday() >= 5:               # weekend, no trading today
        return False
    return now.hour >= PUBLISHED_AFTER_HOUR


def expected_day() -> date:
    """
    The trading day whose prices we ought to be able to get right now.

    Weekends and pre-7pm both point back to the previous weekday. Public
    holidays we cannot know in advance, so they show up as a missing file and
    are reported as such rather than silently skipped over.
    """
    day = india_now().date()
    if not todays_file_should_exist():
        day -= timedelta(days=1)
    while day.weekday() >= 5:            # step back over Sat/Sun
        day -= timedelta(days=1)
    return day


def most_recent(max_days_back: int = 5):
    """
    Return (day, bars, staleness).

    `staleness` is never hidden. It says which day we WANTED, which day we GOT,
    how many days apart they are, and whether that gap is expected or not.

    Nothing here silently substitutes an older day for the one you asked for.
    The caller decides whether stale data is acceptable.
    """
    wanted = expected_day()
    tried = []

    for back in range(max_days_back):
        day = wanted - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        try:
            bars = for_date(day)
            gap = (wanted - day).days
            staleness = {
                "wanted_day": wanted.isoformat(),
                "got_day": day.isoformat(),
                "days_behind": gap,
                "is_stale": gap > 0,
                "note": ("current" if gap == 0 else
                         f"{gap} day(s) behind — likely a market holiday, "
                         f"or the file was not published yet"),
            }
            return day, bars, staleness
        except FetchError as exc:
            tried.append(f"{day.isoformat()}: {str(exc)[:60]}")

    raise FetchError(
        f"no price file found within {max_days_back} days of {wanted.isoformat()} "
        f"— tried {tried}")
