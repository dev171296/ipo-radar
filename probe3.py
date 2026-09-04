"""
Probe 3 — finding a way to get NSE share prices.

Background: NSE's live quote page refuses us (403), but its IPO pages work.
The likely alternative is the "bhavcopy" — a single file NSE publishes each
trading day containing open/high/low/close/volume for every stock. One file
per day beats one request per company, and there is a separate one for SME.

We do not know the current addresses. NSE has changed them over the years, so
this tries every format we know of against the last few trading days and
reports which ones exist.

Nothing here is guessed twice: whatever passes becomes the real code.
"""

from datetime import date, timedelta

from curl_cffi import requests as cffi

TIMEOUT = 25
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def recent_trading_days(count=3):
    """Recent weekdays. Not perfect (ignores holidays) but good enough to test."""
    days, day = [], date.today()
    while len(days) < count:
        day -= timedelta(days=1)
        if day.weekday() < 5:          # Monday=0 ... Friday=4
            days.append(day)
    return days


def candidates_for(d: date):
    """Every bhavcopy address format we know of, for one date."""
    ddmmyyyy = d.strftime("%d%m%Y")
    yyyymmdd = d.strftime("%Y%m%d")
    ddMONyyyy = f"{d.day:02d}{MONTHS[d.month - 1]}{d.year}"
    return {
        "new format (2024+)":
            f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
        "old format":
            f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{d.year}/{MONTHS[d.month - 1]}/cm{ddMONyyyy}bhav.csv.zip",
        "full bhavdata":
            f"https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{ddmmyyyy}.csv",
        "SME bhavcopy":
            f"https://nsearchives.nseindia.com/archives/sme/bhavcopy/"
            f"sme{ddmmyyyy}.csv",
        "delivery data":
            f"https://nsearchives.nseindia.com/archives/equities/mto/"
            f"MTO_{ddmmyyyy}.DAT",
    }


ALT_QUOTE_PATHS = {
    "quote trade_info":
        "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE&section=trade_info",
    "historical securityArchives":
        "https://www.nseindia.com/api/historical/securityArchives"
        "?from=01-09-2026&to=04-09-2026&symbol=RELIANCE&dataType=priceVolumeDeliverable&series=EQ",
    "chart data":
        "https://www.nseindia.com/api/chart-databyindex?index=RELIANCEEQN",
}

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def probe_bhavcopy():
    print("\nQ1 — Which bhavcopy addresses exist?")
    session = cffi.Session(impersonate="chrome")
    # Some archive files still want to look like they came from the site.
    session.headers.update({"Referer": "https://www.nseindia.com/"})

    for d in recent_trading_days(2):
        print(f"\n  --- {d.isoformat()} ---")
        for label, url in candidates_for(d).items():
            try:
                r = session.get(url, timeout=TIMEOUT)
                size = len(r.content)
                # A real bhavcopy is tens of kilobytes. A tiny reply is an error page.
                ok = r.status_code == 200 and size > 5000
                head = r.content[:60]
                record(f"{label} [{d.isoformat()}]", ok,
                       f"{r.status_code} · {size}b · starts {head[:40]!r}")
            except Exception as exc:
                record(f"{label} [{d.isoformat()}]", False,
                       f"{type(exc).__name__}: {str(exc)[:90]}")


def probe_alt_quotes():
    print("\nQ2 — Are any other live-price paths open to us?")
    try:
        session = cffi.Session(impersonate="chrome")
        home = session.get("https://www.nseindia.com", timeout=TIMEOUT)
        print(f"  homepage: {home.status_code}, {len(session.cookies)} cookies")
        if home.status_code != 200:
            record("NSE session", False, "could not get a cookie")
            return
    except Exception as exc:
        record("NSE session", False, f"{type(exc).__name__}: {str(exc)[:90]}")
        return

    for label, url in ALT_QUOTE_PATHS.items():
        try:
            r = session.get(url, headers={"Referer": "https://www.nseindia.com/"},
                            timeout=TIMEOUT)
            body = r.text[:110].replace("\n", " ")
            ok = r.status_code == 200 and body.strip().startswith(("{", "["))
            record(label, ok, f"{r.status_code} · {len(r.text)}b · {body}")
        except Exception as exc:
            record(label, False, f"{type(exc).__name__}: {str(exc)[:90]}")


def main():
    print("=" * 70)
    print("IPO RADAR — PROBE 3: getting share prices out of NSE")
    print("=" * 70)
    probe_bhavcopy()
    probe_alt_quotes()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, ok, _ in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    print(f"\n  {sum(1 for _, ok, _ in results if ok)} of {len(results)} passed.")


if __name__ == "__main__":
    main()
