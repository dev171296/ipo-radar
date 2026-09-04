"""
Source probe for IPO Radar.

Purpose: find out which data sources are actually reachable from a GitHub
Actions runner. Runners use datacenter IP addresses, and several financial
sites treat those differently from a home connection — so the only answer
that counts is the one measured here, not on a laptop.

This script never fails the build. It reports, it does not judge.
"""

import json
import os
import sys
import time

import requests

TIMEOUT = 20

# Look like a real browser. Sites that block scripts mostly check these.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

results = []


def record(source, name, ok, detail):
    results.append((source, name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}: {detail}")


def preview(resp, limit=160):
    """A short, single-line glimpse of a response body."""
    text = resp.text[:limit].replace("\n", " ").replace("\r", " ")
    return f"{resp.status_code} · {len(resp.text)} bytes · {text}"


# ---------------------------------------------------------------- NSE
def probe_nse():
    print("\nNSE (nseindia.com)")
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Step 1 — the "wristband". NSE refuses data requests without a session
    # cookie, and you only get one by visiting the homepage first.
    try:
        home = session.get("https://www.nseindia.com", timeout=TIMEOUT)
        record("nse", "homepage (earn cookie)", home.status_code == 200,
               f"{home.status_code} · {len(session.cookies)} cookies")
        if home.status_code != 200:
            return
    except Exception as exc:
        record("nse", "homepage (earn cookie)", False,
               f"{type(exc).__name__}: {str(exc)[:120]}")
        return

    time.sleep(1)  # be polite

    endpoints = {
        "upcoming issues": "https://www.nseindia.com/api/all-upcoming-issues?category=ipo",
        "current issues": "https://www.nseindia.com/api/ipo-current-issue",
        "SME current": "https://www.nseindia.com/api/ipo-current-issue?type=sme",
        "quote RELIANCE": "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE",
    }
    referer = {"Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"}

    for name, url in endpoints.items():
        try:
            resp = session.get(url, headers=referer, timeout=TIMEOUT)
            ok = resp.status_code == 200 and len(resp.text) > 2
            record("nse", name, ok, preview(resp))
        except Exception as exc:
            record("nse", name, False, f"{type(exc).__name__}: {str(exc)[:120]}")
        time.sleep(1)


# ---------------------------------------------------------------- BSE
def probe_bse():
    print("\nBSE (bseindia.com)")
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    session.headers["Referer"] = "https://www.bseindia.com/"

    endpoints = {
        "IPO listing": "https://api.bseindia.com/BseIndiaAPI/api/GetIPOList/w?Ftype=Equity&Fsub=Active",
        "quote 500325": "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?Debtflag=&scripcode=500325&seriesid=",
    }
    for name, url in endpoints.items():
        try:
            resp = session.get(url, timeout=TIMEOUT)
            ok = resp.status_code == 200 and len(resp.text) > 2
            record("bse", name, ok, preview(resp))
        except Exception as exc:
            record("bse", name, False, f"{type(exc).__name__}: {str(exc)[:120]}")
        time.sleep(1)


# ---------------------------------------------------------------- IPO Guru
def probe_ipoguru():
    print("\nIPO Guru API")
    key = os.environ.get("IPOGURU_KEY", "").strip()
    if not key:
        record("ipoguru", "api", False, "no IPOGURU_KEY set yet — skipped, not a failure")
        return
    try:
        resp = requests.get(
            "https://www.ipoguru.in/api/ipos",
            headers={"x-api-key": key, "User-Agent": "ipo-radar/0.1"},
            timeout=TIMEOUT,
        )
        record("ipoguru", "api", resp.status_code == 200, preview(resp))
    except Exception as exc:
        record("ipoguru", "api", False, f"{type(exc).__name__}: {str(exc)[:120]}")


# ---------------------------------------------------------------- SEBI
def probe_sebi():
    print("\nSEBI filings")
    try:
        resp = requests.get(
            "https://www.sebi.gov.in/filings/public-issues.html",
            headers=BROWSER_HEADERS, timeout=TIMEOUT,
        )
        record("sebi", "public issues page", resp.status_code == 200, preview(resp, 80))
    except Exception as exc:
        record("sebi", "public issues page", False, f"{type(exc).__name__}: {str(exc)[:120]}")


# ---------------------------------------------------------------- Yahoo
def probe_yahoo():
    print("\nYahoo Finance (fallback price source)")
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=5d&interval=1d",
            headers=BROWSER_HEADERS, timeout=TIMEOUT,
        )
        record("yahoo", "chart RELIANCE.NS", resp.status_code == 200, preview(resp, 120))
    except Exception as exc:
        record("yahoo", "chart RELIANCE.NS", False, f"{type(exc).__name__}: {str(exc)[:120]}")


# ---------------------------------------------------------------- news
def probe_news():
    print("\nGoogle News RSS")
    try:
        resp = requests.get(
            "https://news.google.com/rss/search?q=IPO+India&hl=en-IN&gl=IN&ceid=IN:en",
            headers=BROWSER_HEADERS, timeout=TIMEOUT,
        )
        items = resp.text.count("<item>")
        record("news", "rss search", resp.status_code == 200 and items > 0,
               f"{resp.status_code} · {items} articles")
    except Exception as exc:
        record("news", "rss search", False, f"{type(exc).__name__}: {str(exc)[:120]}")


def main():
    print("=" * 68)
    print("IPO RADAR — SOURCE PROBE")
    print("Measuring what is reachable from this GitHub Actions runner.")
    print("=" * 68)

    for probe in (probe_nse, probe_bse, probe_ipoguru, probe_sebi,
                  probe_yahoo, probe_news):
        try:
            probe()
        except Exception as exc:  # a broken probe must not stop the others
            print(f"  probe crashed: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 68)
    print("SUMMARY")
    print("=" * 68)
    passed = sum(1 for *_, ok, _ in [(r[0], r[1], r[2], r[3]) for r in results] if ok)
    for source, name, ok, _ in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {source:9} {name}")
    print(f"\n  {passed} of {len(results)} checks passed.")
    print("\nA FAIL here is information, not a bug. It tells us which rung of")
    print("the fallback ladder this environment actually reaches.")

    # Always exit 0 — this is a measurement, not a test.
    sys.exit(0)


if __name__ == "__main__":
    main()
