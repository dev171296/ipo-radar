"""
Grading the predictions — did the system get it right?

Everything else in this project makes calls. This file is where the calls meet
what actually happened, and it is the only honest way to learn whether any of
it works: whether the scoring rules mean anything, whether Kimi reads better
than Groq, whether "APPLY" is worth more than a coin toss.

Like marking an exam — but only against answers written BEFORE the results
-------------------------------------------------------------------------
Every score and every AI answer is saved with a timestamp and never edited. For
each IPO we find the moment trading began on its listing day, and take the last
answers written before that moment. Anything written afterwards is ignored,
because by then the answer was knowable. That is the whole trick: a prediction
only counts if it was made while the outcome was still unknown.

What happened is read from the same NSE daily price files the collector already
uses, so there is no new source to trust.

What each call is graded against
--------------------------------
    "If applying"  →  the listing-day OPEN against the issue price.
                      That is the listing gain in the usual Indian sense: what
                      you would have made selling the moment it started trading.
    "If holding"   →  the return after 3 months (and 6 and 12 when they come),
                      measured AGAINST THE NIFTY 50 over the same days. A stock
                      that rose 8% while the market rose 10% did not do well; it
                      rode the tide and fell behind it.

Long-term grades are simply "not due" until three months have passed. We do not
grade early, because a verdict on a holding call after one week is noise.

Two honesty rules
-----------------
1. The calls are recomputed with TODAY'S decision rules from the evidence that
   was frozen before listing. So this measures the current system on
   information it could genuinely have had — and says so.
2. AI answers produced by unsuitable models (the web-searching "compound"
   models, the Arabic-language model picked by mistake in September 2026) are
   graded but LABELLED, never deleted. Deleting the embarrassing ones would
   quietly flatter the record. Keeping them, marked, shows how much the proper
   setup improved things.
"""

import glob
import json
import os
import re
from datetime import date, datetime, timedelta

from . import call as calls, storage
from .collectors import bhavcopy
from .evidence import read_price_band

GRADES = os.path.join(storage.DATA, "grades")
PRICE_CACHE = os.path.join(GRADES, "_prices.json")

# How long after an issue closes we look for its first trading day. Indian IPOs
# now list three working days after closing (T+3); ten calendar days leaves
# room for weekends and holidays.
LISTING_WINDOW_DAYS = 10

# Checkpoints after listing, in calendar days. The first trading day on or after
# each is used.
CHECKPOINTS = {"1 week": 7, "1 month": 30, "3 months": 91,
               "6 months": 182, "12 months": 365}
LONG_TERM_CHECKPOINT = "3 months"

# Trading on NSE opens at 09:15 in India = 03:45 UTC. Answers stamped before
# this on listing day were written without knowing how it would open.
MARKET_OPEN_UTC = "034500"

# How many new days of price files one run may download. Each day is two
# files; the cache means each day is only ever fetched once.
MAX_NEW_DAYS_PER_RUN = 45

POSITIVE = {"STRONG APPLY", "APPLY", "APPLY SELECTIVELY"}
NEGATIVE = {"WAIT FOR LISTING", "AVOID"}

# An AI score is a directional view only when it commits: 55 and above is a
# "yes", 45 and below a "no". Between is "no strong view" and is not graded —
# rewarding a model for sitting on the fence would be marking vagueness.
AI_YES, AI_NO = 55, 45

UNSUITABLE = ("compound", "allam")      # web-enabled or wrong-language models
NIFTY = "Nifty 50"
INDEX_FILE = ("https://nsearchives.nseindia.com/content/indices/"
              "ind_close_all_{ddmmyyyy}.csv")


# ------------------------------------------------------------ price files

class Prices:
    """
    Daily prices for the symbols we track, cached so each day is fetched once.

    The cache keeps only OUR symbols, not the 4,000 others in each file, so it
    stays a few kilobytes. A date with no file (a weekend or a market holiday)
    is remembered as such — but only once it is safely in the past, so today's
    not-yet-published file is never mistaken for a holiday.
    """

    def __init__(self, symbols):
        self.symbols = {s.upper() for s in symbols if s}
        self.cache = {}
        self.new_days = 0
        self.tried = set()        # days already attempted in THIS run
        if os.path.exists(PRICE_CACHE):
            try:
                with open(PRICE_CACHE, encoding="utf-8") as handle:
                    self.cache = json.load(handle)
            except (OSError, ValueError):
                self.cache = {}

    def day(self, when: date, need: str = None):
        """
        {SYMBOL: {open, close}} for one date, or None if no trading.

        `need` names the stock being asked about. A cached day is re-fetched
        only if it has never been checked for THAT stock — otherwise every new
        IPO would trigger a re-download of every day already on file.
        """
        key = when.isoformat()
        entry = self.cache.get(key)
        if entry is not None:
            if entry.get("closed"):
                return None
            known = entry.get("symbols") or {}
            checked = set(known) | set(entry.get("absent") or [])
            if need is None or need in checked:
                return known
        if when >= date.today() or key in self.tried:
            return None
        self.tried.add(key)
        if self.new_days >= MAX_NEW_DAYS_PER_RUN:
            raise TimeoutError("price-file allowance for this run used up")

        self.new_days += 1
        try:
            bars = bhavcopy.for_date(when, quiet=True)
        except Exception:
            # No file for a day comfortably in the past = no trading that day.
            if when < date.today() - timedelta(days=3):
                self.cache[key] = {"closed": True}
            return None

        found = {s: {"open": bars[s].get("open"), "close": bars[s].get("close"),
                     "segment": bars[s].get("segment")}
                 for s in self.symbols if s in bars}
        nifty = (entry or {}).get("nifty") or _nifty_close(when)
        self.cache[key] = {"symbols": found,
                           "absent": sorted(self.symbols - set(found)),
                           "nifty": nifty}
        return found

    def nifty(self, when: date):
        entry = self.cache.get(when.isoformat()) or {}
        return entry.get("nifty")

    def first_trading_day_from(self, start: date, need=None, limit_days=10):
        """The first date on or after `start` with a price file."""
        for offset in range(limit_days):
            when = start + timedelta(days=offset)
            if when >= date.today():
                return None
            if self.day(when, need) is not None:
                return when
        return None

    def save(self):
        os.makedirs(GRADES, exist_ok=True)
        with open(PRICE_CACHE, "w", encoding="utf-8") as handle:
            json.dump(self.cache, handle, sort_keys=True)


def _nifty_close(when: date):
    """The Nifty 50 close for a day, from NSE's daily index file, or None."""
    import csv
    import io
    from .http import chrome_session
    try:
        session = chrome_session()
        session.headers.update({"Referer": "https://www.nseindia.com/"})
        resp = session.get(INDEX_FILE.format(ddmmyyyy=when.strftime("%d%m%Y")),
                           timeout=30)
        if resp.status_code != 200:
            return None
        for row in csv.DictReader(io.StringIO(resp.text)):
            tidy = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if tidy.get("index name", "").lower() == NIFTY.lower():
                value = tidy.get("closing index value")
                return float(value.replace(",", "")) if value else None
    except Exception:
        return None
    return None


# -------------------------------------------------- what we said, and when

def _before(paths, cutoff_stamp):
    """The latest snapshot stamped strictly before the cutoff."""
    stamped = []
    for path in paths:
        match = re.search(r"(\d{14})\.json$", path)
        if match and match.group(1) < cutoff_stamp:
            stamped.append((match.group(1), path))
    if not stamped:
        return None, None
    stamp, path = max(stamped)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), stamp
    except (OSError, ValueError):
        return None, None


def predictions_before(ipo_id: str, listing: date) -> dict:
    """
    Everything the system had said about this IPO before trading began.

    Our own score, the two AI readings, and the call those produce under
    today's rules — all taken from snapshots written before 09:15 IST on
    listing day.
    """
    cutoff = listing.strftime("%Y%m%d") + MARKET_OPEN_UTC
    score, score_at = _before(
        glob.glob(os.path.join(storage.DATA, "scores", ipo_id, "*.json")), cutoff)
    comparison, _ = _before(
        glob.glob(os.path.join(storage.DATA, "analysis", ipo_id,
                               "comparison-*.json")), cutoff)

    models = {}
    for family in ("groq", "nvidia", "gemini"):
        answer, at = _before(
            glob.glob(os.path.join(storage.DATA, "analysis", ipo_id,
                                   f"{family}-*.json")), cutoff)
        if not answer or not answer.get("ok") or not answer.get("answer"):
            continue
        name = answer.get("model") or family
        body = answer["answer"]
        models[family] = {
            "model": name,
            "answered_at": at,
            "listing_view": (body.get("listing_view") or {}).get("score"),
            "longterm_view": (body.get("longterm_view") or {}).get("score"),
            "unsuitable_model": any(word in name.lower() for word in UNSUITABLE),
        }

    decision = calls.decide(score, comparison) if score else None
    return {
        "cutoff_utc": cutoff,
        "score_taken_at": score_at,
        "fundamentals": ((score or {}).get("fundamentals") or {}).get("score"),
        "call": decision,
        "models": models,
    }


# ----------------------------------------------------------------- marking

def _direction_of_call(label):
    if label in POSITIVE:
        return "positive"
    if label in NEGATIVE:
        return "negative"
    return None                       # NOT ENOUGH INFORMATION is not a call


def _direction_of_score(value):
    if value is None:
        return None
    if value >= AI_YES:
        return "positive"
    if value <= AI_NO:
        return "negative"
    return None


def _right(direction, outcome):
    """A positive view is right if the outcome was a gain; negative, if not."""
    if direction is None or outcome is None:
        return None
    return (outcome > 0) if direction == "positive" else (outcome <= 0)


def grade_one(record: dict, prices: Prices) -> dict:
    """Find the listing, measure what happened, and mark every prediction."""
    ipo_id = record["id"]
    symbol = (record.get("symbol") or "").upper()
    close = (record.get("dates") or {}).get("close")
    result = {"ipo_id": ipo_id, "name": record.get("name"), "symbol": symbol,
              "graded_at": storage.now()}

    if not symbol or not close:
        result["status"] = "no symbol or closing date"
        return result
    closed_on = date.fromisoformat(close)
    if closed_on >= date.today():
        result["status"] = "issue still open"
        return result

    # --- when did it list, and at what price? ---------------------------------
    listing = None
    for offset in range(1, LISTING_WINDOW_DAYS + 1):
        when = closed_on + timedelta(days=offset)
        if when >= date.today():
            break
        bars = prices.day(when, symbol)
        if bars and symbol in bars:
            listing = when
            break
    if not listing:
        result["status"] = ("not listed yet" if
                            (date.today() - closed_on).days <= LISTING_WINDOW_DAYS
                            else "no listing found in the price files")
        return result

    _, issue_price = read_price_band(record.get("price_band"))
    first = prices.day(listing, symbol)[symbol]
    result.update({
        "status": "listed",
        "listed_on": listing.isoformat(),
        "issue_price": issue_price,
        "issue_price_note": "top of the price band — the price nearly every "
                            "Indian IPO is finally set at",
        "listing_open": first.get("open"),
        "listing_close": first.get("close"),
    })
    if not issue_price:
        result["status"] = "listed, but no issue price to measure against"
        return result

    listing_gain = (first["open"] - issue_price) / issue_price if first.get("open") else None
    result["listing_gain_pct"] = round(listing_gain * 100, 2) if listing_gain is not None else None
    result["listing_day_close_pct"] = round(
        (first["close"] - issue_price) / issue_price * 100, 2)

    # --- later checkpoints, against the Nifty over the same days ----------------
    nifty_start = prices.nifty(listing)
    result["checkpoints"] = {}
    for label, days in CHECKPOINTS.items():
        target = listing + timedelta(days=days)
        if target >= date.today():
            result["checkpoints"][label] = {"due": target.isoformat()}
            continue
        on = prices.first_trading_day_from(target, symbol)
        bars = prices.day(on, symbol) if on else None
        if not bars or symbol not in bars:
            result["checkpoints"][label] = {"due": target.isoformat(),
                                            "note": "no price found"}
            continue
        price = bars[symbol]["close"]
        stock = (price - issue_price) / issue_price
        entry = {"date": on.isoformat(), "close": price,
                 "return_pct": round(stock * 100, 2)}
        nifty_then = prices.nifty(on)
        if nifty_start and nifty_then:
            market = (nifty_then - nifty_start) / nifty_start
            entry["nifty_pct"] = round(market * 100, 2)
            entry["vs_nifty_pct"] = round((stock - market) * 100, 2)
        result["checkpoints"][label] = entry

    # --- mark what we said ---------------------------------------------------
    said = predictions_before(ipo_id, listing)
    result["predictions"] = said
    marks = {}

    decision = said.get("call") or {}
    listing_call = (decision.get("listing") or {}).get("call")
    holding_call = (decision.get("longterm") or {}).get("call")
    long_term = result["checkpoints"].get(LONG_TERM_CHECKPOINT, {})
    long_outcome = long_term.get("vs_nifty_pct", long_term.get("return_pct"))

    marks["our_call"] = {
        "listing": {"said": listing_call,
                    "right": _right(_direction_of_call(listing_call), listing_gain)},
        "holding": {"said": holding_call,
                    "right": _right(_direction_of_call(holding_call), long_outcome)
                    if long_outcome is not None else None,
                    "due": None if long_outcome is not None else long_term.get("due")},
    }
    for family, body in said.get("models", {}).items():
        marks[family] = {
            "model": body["model"],
            "unsuitable_model": body["unsuitable_model"],
            "listing": {"said": body.get("listing_view"),
                        "right": _right(_direction_of_score(body.get("listing_view")),
                                        listing_gain)},
            "holding": {"said": body.get("longterm_view"),
                        "right": _right(_direction_of_score(body.get("longterm_view")),
                                        long_outcome)
                        if long_outcome is not None else None},
        }
    result["marks"] = marks
    return result


# ------------------------------------------------------------ the scoreboard

def scoreboard(results: list) -> dict:
    """
    How often each predictor was right, and — more useful than a hit rate —
    how the IPOs it liked actually did compared with the ones it did not.

    A predictor that is "right 60% of the time" in a hot market where 80% of
    IPOs listed at a gain is worse than useless. So beside every hit rate we
    show the base rate (how often simply saying "yes" would have been right),
    and the average listing gain of its yes-calls against its no-calls. A good
    predictor has a clear gap between those two numbers.
    """
    listed = [r for r in results if r.get("listing_gain_pct") is not None]
    gains = [r["listing_gain_pct"] for r in listed]
    board = {
        "built_at": storage.now(),
        "listed_ipos": len(listed),
        "base_rate_pct": round(sum(1 for g in gains if g > 0) / len(gains) * 100)
        if gains else None,
        "average_listing_gain_pct": round(sum(gains) / len(gains), 1) if gains else None,
        "predictors": {},
        "long_term_note": (f"graded {LONG_TERM_CHECKPOINT} after listing, "
                           f"against the Nifty 50"),
    }

    names = {"our_call": "Our call (rules + both AIs)", "groq": "Groq",
             "nvidia": "NVIDIA (Kimi K3)", "gemini": "Gemini (retired)"}
    for key, label in names.items():
        for clean in (True, False):
            rows = []
            for r in listed:
                mark = (r.get("marks") or {}).get(key)
                if not mark:
                    continue
                if key != "our_call" and mark.get("unsuitable_model") == clean:
                    continue
                rows.append((mark, r["listing_gain_pct"]))
            if key == "our_call" and not clean:
                continue
            if not rows:
                continue

            judged = [(m, g) for m, g in rows if m["listing"]["right"] is not None]
            yes = [g for m, g in judged
                   if (m["listing"]["said"] in POSITIVE if key == "our_call"
                       else (m["listing"]["said"] or 0) >= AI_YES)]
            no = [g for m, g in judged
                  if (m["listing"]["said"] in NEGATIVE if key == "our_call"
                      else (m["listing"]["said"] or 100) <= AI_NO)]
            held = [m["holding"]["right"] for m, _ in rows
                    if m["holding"]["right"] is not None]

            entry = {
                "listing_graded": len(judged),
                "listing_right": sum(1 for m, _ in judged if m["listing"]["right"]),
                "no_view": len(rows) - len(judged),
                "avg_gain_when_yes": round(sum(yes) / len(yes), 1) if yes else None,
                "avg_gain_when_no": round(sum(no) / len(no), 1) if no else None,
                "holding_graded": len(held),
                "holding_right": sum(1 for h in held if h),
            }
            if entry["listing_graded"]:
                entry["listing_hit_rate_pct"] = round(
                    entry["listing_right"] / entry["listing_graded"] * 100)
            suffix = "" if clean else " — unsuitable models (labelled, kept)"
            board["predictors"][label + suffix] = entry
    return board


# ------------------------------------------------------------------ running

def run() -> dict:
    """Grade every IPO whose issue has closed. Called once per collector run."""
    records = storage.all_records()
    prices = Prices([r.get("symbol") for r in records])
    results = []
    try:
        for record in sorted(records, key=lambda r: (r.get("dates") or {}).get("close") or ""):
            try:
                results.append(grade_one(record, prices))
            except TimeoutError:
                results.append({"ipo_id": record["id"], "name": record.get("name"),
                                "status": "price files not fetched yet — next run"})
    finally:
        prices.save()

    os.makedirs(GRADES, exist_ok=True)
    for result in results:
        with open(os.path.join(GRADES, f"{result['ipo_id']}.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(result, handle, indent=1, ensure_ascii=False)
    board = scoreboard(results)
    board["per_ipo"] = [
        {"ipo_id": r["ipo_id"], "name": r.get("name"), "status": r.get("status"),
         "listed_on": r.get("listed_on"), "listing_gain_pct": r.get("listing_gain_pct"),
         "our_listing_call": (((r.get("marks") or {}).get("our_call") or {})
                              .get("listing") or {}).get("said"),
         "right": (((r.get("marks") or {}).get("our_call") or {})
                   .get("listing") or {}).get("right")}
        for r in results]
    with open(os.path.join(GRADES, "scoreboard.json"), "w", encoding="utf-8") as handle:
        json.dump(board, handle, indent=1, ensure_ascii=False)
    return board
