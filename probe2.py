"""
Source probe, round 2.

Round 1 told us:
  - NSE returns 403 from a GitHub runner (plain `requests`)
  - Yahoo returns 429 from a GitHub runner
  - BSE quotes work, SEBI works, Google News works
  - The BSE "IPO list" endpoint returned HTML, not JSON — wrong path

So round 2 asks three questions:
  1. Does TLS impersonation get us past NSE and Yahoo? Sites like these
     fingerprint the TLS handshake itself, and plain Python has an obviously
     non-browser fingerprint. curl_cffi mimics Chrome's handshake exactly.
  2. Is NSE's archive subdomain less defended than the main site?
  3. Which BSE endpoint actually returns the IPO calendar as JSON?

New rule after round 1: a 200 is not a pass. We check the body looks like
what we asked for.
"""

import json
import os
import sys
import time

import requests
from curl_cffi import requests as cffi

TIMEOUT = 25

# If WARP is running on this runner it exposes a local proxy. When set, every
# request below goes out through Cloudflare's network instead of the runner's
# own datacenter IP. Devanshu's earlier project found that the IP change AND
# the Chrome TLS fingerprint are both required — either alone fails.
PROXY = os.environ.get("WARP_PROXY", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

results = []


def show_egress():
    """Prove which IP we are actually leaving from."""
    print("\nEgress check")
    print(f"  WARP_PROXY = {PROXY or '(not set — direct connection)'}")
    for label, prox in [("direct", None)] + ([("via WARP", PROXIES)] if PROXIES else []):
        try:
            r = requests.get("https://cloudflare.com/cdn-cgi/trace",
                             proxies=prox, timeout=TIMEOUT)
            info = dict(line.split("=", 1) for line in r.text.strip().splitlines()
                        if "=" in line)
            print(f"  {label:9} ip={info.get('ip','?')} loc={info.get('loc','?')} warp={info.get('warp','?')}")
        except Exception as exc:
            note = ""
            if "SOCKS" in str(exc):
                note = "  <-- OUR BUG: PySocks missing or wrong proxy scheme"
            print(f"  {label:9} FAILED {type(exc).__name__}: {str(exc)[:90]}{note}")


def looks_like_json(text):
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
        return True
    except Exception:
        return False


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def describe(resp, want_json=True):
    """Report status AND whether the body is what we asked for."""
    text = resp.text
    if want_json:
        good = resp.status_code == 200 and looks_like_json(text)
        kind = "JSON" if looks_like_json(text) else "not-JSON"
    else:
        good = resp.status_code == 200 and len(text) > 500
        kind = "html"
    snippet = text[:150].replace("\n", " ").replace("\r", " ")
    return good, f"{resp.status_code} · {len(text)}b · {kind} · {snippet}"


# ------------------------------------------------- Q1: TLS impersonation
def q1_impersonation():
    print("\nQ1 — Does Chrome TLS impersonation get past the blocks?")

    # NSE, the whole two-step dance but with a Chrome TLS fingerprint
    try:
        s = cffi.Session(impersonate="chrome", proxies=PROXIES)
        home = s.get("https://www.nseindia.com", timeout=TIMEOUT)
        record("NSE homepage (impersonated)", home.status_code == 200,
               f"{home.status_code} · {len(s.cookies)} cookies")
        if home.status_code == 200:
            time.sleep(1)
            for name, url in [
                ("NSE upcoming issues",
                 "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"),
                ("NSE current issues",
                 "https://www.nseindia.com/api/ipo-current-issue"),
                ("NSE quote RELIANCE",
                 "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE"),
            ]:
                r = s.get(url, headers={"Referer": "https://www.nseindia.com/"},
                          timeout=TIMEOUT)
                ok, detail = describe(r)
                record(name, ok, detail)
                time.sleep(1)
    except Exception as exc:
        record("NSE (impersonated)", False, f"{type(exc).__name__}: {str(exc)[:130]}")

    # Yahoo — was 429 with plain requests. Is that bot detection or real limiting?
    try:
        s = cffi.Session(impersonate="chrome", proxies=PROXIES)
        r = s.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS"
            "?range=5d&interval=1d", timeout=TIMEOUT)
        ok, detail = describe(r)
        record("Yahoo chart (impersonated)", ok, detail)
    except Exception as exc:
        record("Yahoo (impersonated)", False, f"{type(exc).__name__}: {str(exc)[:130]}")


# ------------------------------------------------- Q2: NSE archives
def q2_archives():
    print("\nQ2 — Is NSE's archive subdomain less defended?")
    for name, url in [
        ("nsearchives root", "https://nsearchives.nseindia.com/"),
        ("NSE SME site", "https://www.nseindia.com/emerge"),
    ]:
        for label, getter in [("plain", None), ("impersonated", "chrome")]:
            try:
                if getter:
                    r = cffi.Session(impersonate=getter, proxies=PROXIES).get(url, timeout=TIMEOUT)
                else:
                    r = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT, proxies=PROXIES)
                record(f"{name} ({label})", r.status_code == 200,
                       f"{r.status_code} · {len(r.text)}b")
            except Exception as exc:
                record(f"{name} ({label})", False,
                       f"{type(exc).__name__}: {str(exc)[:100]}")
            time.sleep(1)


# ------------------------------------------------- Q3: BSE IPO calendar
def q3_bse_ipo():
    print("\nQ3 — Which BSE endpoint gives the IPO calendar as JSON?")
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    s.headers["Referer"] = "https://www.bseindia.com/"

    candidates = {
        "GetIPOList Active":
            "https://api.bseindia.com/BseIndiaAPI/api/GetIPOList/w?Ftype=Equity&Fsub=Active",
        "PublicIssue w":
            "https://api.bseindia.com/BseIndiaAPI/api/PublicIssue/w?Type=IPO",
        "IPODtls":
            "https://api.bseindia.com/BseIndiaAPI/api/IPODtls/w?Ftype=Equity",
        "DisplayIPO":
            "https://api.bseindia.com/BseIndiaAPI/api/DisplayIPO/w?Ftype=Equity&Fsub=Active",
        "IPOSubscription":
            "https://api.bseindia.com/BseIndiaAPI/api/IPOSubscription/w?scripcode=&type=",
        "SME IPO list":
            "https://api.bseindia.com/BseIndiaAPI/api/GetIPOList/w?Ftype=SME&Fsub=Active",
    }
    for name, url in candidates.items():
        try:
            r = s.get(url, timeout=TIMEOUT, proxies=PROXIES)
            ok, detail = describe(r)
            record(f"BSE {name}", ok, detail)
        except Exception as exc:
            record(f"BSE {name}", False, f"{type(exc).__name__}: {str(exc)[:100]}")
        time.sleep(1)

    # The human-facing page, as a scraping fallback
    try:
        r = s.get("https://www.bseindia.com/markets/PublicIssues/IPOIssues_new.aspx",
                  timeout=TIMEOUT, proxies=PROXIES)
        ok, detail = describe(r, want_json=False)
        record("BSE public issues page (HTML)", ok, detail)
    except Exception as exc:
        record("BSE public issues page (HTML)", False,
               f"{type(exc).__name__}: {str(exc)[:100]}")


# ------------------------------------------------- IPO Guru
def q4_ipoguru():
    print("\nQ4 — IPO Guru API")
    key = os.environ.get("IPOGURU_KEY", "").strip()
    if not key:
        record("IPO Guru", False, "no key yet — skipped")
        return
    for header in ({"x-api-key": key}, {"Authorization": f"Bearer {key}"}):
        try:
            r = requests.get("https://www.ipoguru.in/api/ipos",
                             headers={**header, "User-Agent": "ipo-radar/0.1"},
                             timeout=TIMEOUT, proxies=PROXIES)
            ok, detail = describe(r)
            record(f"IPO Guru ({list(header)[0]})", ok, detail)
        except Exception as exc:
            record("IPO Guru", False, f"{type(exc).__name__}: {str(exc)[:100]}")


def main():
    print("=" * 70)
    print("IPO RADAR — SOURCE PROBE 2")
    print("TLS impersonation + optional WARP proxy, NSE archives, BSE endpoints.")
    print("=" * 70)

    show_egress()

    for probe in (q1_impersonation, q2_archives, q3_bse_ipo, q4_ipoguru):
        try:
            probe()
        except Exception as exc:
            print(f"  probe crashed: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, ok, _ in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    print(f"\n  {sum(1 for _, ok, _ in results if ok)} of {len(results)} passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
