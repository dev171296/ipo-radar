"""
Prices — the fallback ladder.

Order, decided by measurement not preference:

  1. BSE   — plain requests, worked first try, no impersonation needed
  2. Yahoo — chart API called directly with a Chrome fingerprint
  3. (a third rung can be added here later if both ever fail together)

NSE is deliberately NOT a rung: its quote endpoint returns 403 even on a
session where its IPO endpoints return 200.

Two rules carried over from Devanshu's earlier project, both learned the hard way:
  - Never use the `yfinance` library. It breaks against curl_cffi and returns
    a confident "possibly delisted" — a silent wrong answer, worse than an error.
    We call the chart API ourselves and read the JSON.
  - Never call curl_cffi from threads. Single-threaded only.

The same company is named differently by each source, so a stock we track has
to carry all three identifiers or the ladder cannot fall.
"""

from ..http import FetchError, chrome_session, get_json, plain_session

BSE_QUOTE = ("https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
             "?Debtflag=&scripcode={code}&seriesid=")
YAHOO_CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               "?range={range}&interval=1d")


def _sane(bar: dict) -> bool:
    """
    A reply can be well-formed and still wrong. Check it makes sense as a price
    before we believe it.
    """
    close = bar.get("close")
    if close is None or not isinstance(close, (int, float)) or close <= 0:
        return False
    for key in ("open", "high", "low"):
        v = bar.get(key)
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            return False
    if bar.get("high") is not None and bar.get("low") is not None:
        if bar["high"] < bar["low"]:
            return False
    return True


# ---------------------------------------------------------------- rung 1: BSE
def from_bse(scrip_code: str) -> dict:
    session = plain_session()
    session.headers["Referer"] = "https://www.bseindia.com/"
    data = get_json(session, BSE_QUOTE.format(code=scrip_code), what="BSE quote")

    rate = (data or {}).get("CurrRate") or {}
    try:
        close = float(rate.get("LTP"))
    except (TypeError, ValueError):
        raise FetchError("BSE quote had no usable LTP")

    bar = {"close": close, "open": None, "high": None, "low": None,
           "volume": None, "source": "bse"}
    if not _sane(bar):
        raise FetchError(f"BSE returned an implausible price: {close}")
    return bar


# -------------------------------------------------------------- rung 2: Yahoo
def from_yahoo(symbol: str, days: str = "5d") -> dict:
    session = chrome_session()
    data = get_json(session, YAHOO_CHART.format(symbol=symbol, range=days),
                    what="Yahoo chart")

    try:
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        i = len(result["timestamp"]) - 1        # newest bar
        bar = {
            "open": quote["open"][i],
            "high": quote["high"][i],
            "low": quote["low"][i],
            "close": quote["close"][i],
            "volume": quote["volume"][i],
            "source": "yahoo",
        }
    except (KeyError, IndexError, TypeError) as exc:
        raise FetchError(f"Yahoo chart shape unexpected: {type(exc).__name__}")

    if not _sane(bar):
        raise FetchError(f"Yahoo returned an implausible bar: {bar}")
    return bar


# ---------------------------------------------------------------- the ladder
def latest_price(ids: dict) -> dict:
    """
    Try each rung in order. First sane answer wins.

    `ids` carries the three names for one company, e.g.
        {"bse_code": "500325", "yahoo": "RELIANCE.NS", "nse": "RELIANCE"}

    Returns the bar plus which rung answered and which rungs were tried,
    so the dashboard can always show where a number came from.
    """
    attempts = []

    for label, fn, key in [
        ("bse", from_bse, "bse_code"),
        ("yahoo", from_yahoo, "yahoo"),
    ]:
        identifier = ids.get(key)
        if not identifier:
            attempts.append({"rung": label, "result": "no identifier for this source"})
            continue
        try:
            bar = fn(identifier)
            bar["attempts"] = attempts + [{"rung": label, "result": "ok"}]
            return bar
        except Exception as exc:
            attempts.append({"rung": label,
                             "result": f"{type(exc).__name__}: {str(exc)[:100]}"})

    raise FetchError(f"every price source failed: {attempts}")
