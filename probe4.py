"""
Probe 4 — can we find and read an IPO's prospectus?

We need the official document each company files before listing. It contains
the accounts, the peer comparison, what the money is for, and the risks.

Three things we do not know yet:
  1. Where the document lives. SEBI publishes filings; NSE also links them.
  2. Whether our companies appear under the names we hold. "Kanohar
     Electricals Limited" may be filed as something slightly different.
  3. Whether a 500-page PDF can be downloaded and read from a GitHub runner.

This probe finds out. It downloads nothing large unless a link is found.
"""

import io
import re

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

TIMEOUT = 30

# The five IPOs our collector is currently tracking.
OUR_COMPANIES = [
    "Kanohar Electricals",
    "Prasol Chemicals",
    "Glass Wall Systems",
    "Pranav Constructions",
    "Qualiance International",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def looks_like_ours(text):
    """Does this text mention any company we are tracking?"""
    lowered = (text or "").lower()
    return [c for c in OUR_COMPANIES if c.split()[0].lower() in lowered]


# ------------------------------------------------------------ SEBI
SEBI_PAGES = {
    "RHP filed with ROC":
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
        "?doListing=yes&sid=3&ssid=15&smid=11",
    "Draft offer documents":
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
        "?doListing=yes&sid=3&ssid=15&smid=10",
    "Public issues hub":
        "https://www.sebi.gov.in/filings/public-issues.html",
}


def probe_sebi():
    print("\nQ1 — What does SEBI's filings site give us?")
    session = requests.Session()
    session.headers.update(HEADERS)

    for label, url in SEBI_PAGES.items():
        try:
            r = session.get(url, timeout=TIMEOUT)
            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.find_all("a", href=True)
            pdfs = [a for a in links if ".pdf" in a["href"].lower()]
            company_links = [a for a in links if looks_like_ours(a.get_text())]

            ok = r.status_code == 200 and len(links) > 20
            record(label, ok,
                   f"{r.status_code} · {len(r.text)}b · {len(links)} links · "
                   f"{len(pdfs)} PDFs · {len(company_links)} of ours")

            for a in company_links[:3]:
                print(f"        FOUND: {a.get_text()[:60].strip()} -> {a['href'][:80]}")
            # Show a couple of ordinary rows so we can see the page's shape.
            for a in links[:0] or pdfs[:2]:
                print(f"        sample PDF: {a.get_text()[:50].strip()} -> {a['href'][:80]}")
        except Exception as exc:
            record(label, False, f"{type(exc).__name__}: {str(exc)[:110]}")


# ------------------------------------------------------------ NSE
def probe_nse_prospectus():
    print("\nQ2 — Does NSE link the prospectus for an IPO?")
    try:
        s = cffi.Session(impersonate="chrome")
        home = s.get("https://www.nseindia.com", timeout=TIMEOUT)
        if home.status_code != 200:
            record("NSE session", False, f"homepage {home.status_code}")
            return
        print(f"  session ok, {len(s.cookies)} cookies")
    except Exception as exc:
        record("NSE session", False, f"{type(exc).__name__}: {str(exc)[:100]}")
        return

    # Candidate endpoints that might carry documents for a live issue.
    candidates = {
        "ipo detail (KANOHAR)":
            "https://www.nseindia.com/api/ipo-detail?symbol=KANOHAR",
        "ipo active category":
            "https://www.nseindia.com/api/ipo-active-category?symbol=QUALIANCE",
        "public issues archive":
            "https://www.nseindia.com/api/public-past-issues",
    }
    for label, url in candidates.items():
        try:
            r = s.get(url, headers={"Referer": "https://www.nseindia.com/"},
                      timeout=TIMEOUT)
            body = r.text[:200].replace("\n", " ")
            ok = r.status_code == 200 and body.strip().startswith(("{", "["))
            record(label, ok, f"{r.status_code} · {len(r.text)}b · {body[:130]}")
        except Exception as exc:
            record(label, False, f"{type(exc).__name__}: {str(exc)[:100]}")


# ------------------------------------------------------------ can we read a PDF?
def probe_pdf_reading():
    """
    Prove we can download a PDF and get words out of it, using a document we
    know exists. If this fails, nothing downstream can work.
    """
    print("\nQ3 — Can we download a PDF and read text from it?")
    url = ("https://www.sebi.gov.in/sebi_data/commondocs/jun-2026/"
           "National%20Stock%20Exchange%20of%20India%20Ltd%20-%20Abridged_p.pdf")
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        if r.status_code != 200 or len(r.content) < 10000:
            record("download a known PDF", False,
                   f"{r.status_code} · {len(r.content)}b")
            return
        record("download a known PDF", True, f"{r.status_code} · {len(r.content)}b")

        import pdfplumber
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            pages = len(pdf.pages)
            text = (pdf.pages[0].extract_text() or "")[:200].replace("\n", " ")
        record("read text out of it", bool(text.strip()),
               f"{pages} pages · first page starts: {text[:120]}")
    except Exception as exc:
        record("PDF handling", False, f"{type(exc).__name__}: {str(exc)[:130]}")


def main():
    print("=" * 70)
    print("IPO RADAR — PROBE 4: finding and reading the prospectus")
    print("=" * 70)
    for probe in (probe_sebi, probe_nse_prospectus, probe_pdf_reading):
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


if __name__ == "__main__":
    main()
