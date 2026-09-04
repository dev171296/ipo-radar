"""
Finding each IPO's prospectus on SEBI's site.

Verified 4 Sep 2026: SEBI's "Red Herring Documents filed with ROC" page listed
23 documents, eight of which belonged to companies we track. Matching by name
worked first try.

Each company gets two entries on that page:

  "<COMPANY> - RHP <COMPANY>"          -> a SEBI detail page holding the full
                                          prospectus (400-600 pages)
  "<COMPANY> - Abridged Prospectus"    -> a PDF directly (~30 pages)

The abridged one is the regulator-mandated summary: financials, what the money
is for, key risks, promoters, peer comparison. We take it for every IPO because
it is small and reliable. The full document we take as well, for the litigation
and related-party detail the summary leaves out.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import TIMEOUT, FetchError, plain_session
from ..identity import normalise

LISTING_PAGES = {
    "rhp": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
           "?doListing=yes&sid=3&ssid=15&smid=11",
    "draft": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
             "?doListing=yes&sid=3&ssid=15&smid=10",
}

BASE = "https://www.sebi.gov.in"


def _kind_of(link_text: str) -> str:
    """What sort of document is this link?"""
    lowered = (link_text or "").lower()
    if "abridged" in lowered:
        return "abridged"
    if "rhp" in lowered or "red herring" in lowered:
        return "rhp"
    if "drhp" in lowered or "draft" in lowered:
        return "drhp"
    return "other"


def _company_from(link_text: str) -> str:
    """
    Pull the company name out of a link caption.

    Captions look like "KANOHAR ELECTRICALS LIMITED - RHP KANOHAR ELECTRICALS
    LIMITED", so everything before the first dash is the name.
    """
    return (link_text or "").split(" - ")[0].strip()


def list_filings(which: str = "rhp") -> list:
    """
    Every document currently listed on one of SEBI's filing pages.

    Returns [{company, normalised, kind, url, caption}].
    """
    url = LISTING_PAGES.get(which)
    if not url:
        raise FetchError(f"unknown SEBI page: {which}")

    session = plain_session()
    resp = session.get(url, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise FetchError(f"SEBI {which} page: HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    filings = []

    for anchor in soup.find_all("a", href=True):
        caption = " ".join(anchor.get_text().split())
        if " - " not in caption:
            continue
        company = _company_from(caption)
        if len(company) < 4:
            continue
        filings.append({
            "company": company,
            "normalised": normalise(company),
            "kind": _kind_of(caption),
            "url": urljoin(BASE, anchor["href"]),
            "caption": caption,
        })
    return filings


def _pdf_links_on(page_url: str) -> list:
    """
    Open a SEBI detail page and collect the PDF links it holds.

    The full prospectus is not linked directly from the listing — the listing
    points at a page, and the page points at the document.
    """
    session = plain_session()
    resp = session.get(page_url, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise FetchError(f"SEBI detail page: HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    found = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if ".pdf" in href.lower():
            found.append({
                "url": urljoin(BASE, href),
                "caption": " ".join(anchor.get_text().split())[:120],
            })
    return found


def documents_for(company_name: str, filings: list = None) -> dict:
    """
    Find the abridged prospectus and the full prospectus for one company.

    Matching uses the normalised name — the same machinery that copes with
    "Limited" versus "Ltd" elsewhere in the system.

    Returns {"abridged": url|None, "full": url|None, "matched_as": name|None,
             "notes": [...]}.
    """
    filings = filings if filings is not None else list_filings("rhp")
    wanted = normalise(company_name)
    notes = []

    mine = [f for f in filings if f["normalised"] == wanted]
    if not mine:
        # Fall back to a looser match: same first two significant words.
        head = " ".join(wanted.split()[:2])
        mine = [f for f in filings if f["normalised"].startswith(head)] if head else []
        if mine:
            notes.append(f"loose name match on '{head}'")

    if not mine:
        return {"abridged": None, "full": None, "matched_as": None,
                "notes": ["not found on SEBI's current filings page"]}

    notes.append("matched " + str(len(mine)) + " filing(s): "
                 + ", ".join(f"{f['kind']}:{f['caption'][:40]}" for f in mine[:4]))
    matched_as = mine[0]["company"]
    result = {"abridged": None, "full": None, "matched_as": matched_as, "notes": notes}

    for filing in mine:
        if filing["kind"] == "abridged" and filing["url"].lower().endswith(".pdf"):
            result["abridged"] = filing["url"]
        elif filing["kind"] == "rhp":
            if filing["url"].lower().endswith(".pdf"):
                result["full"] = filing["url"]
            else:
                # A detail page: follow it to find the document itself.
                try:
                    pdfs = _pdf_links_on(filing["url"])
                    if pdfs:
                        # Prefer a link that is not the abridged version.
                        main = [p for p in pdfs
                                if "abridged" not in p["caption"].lower()]
                        chosen = (main or pdfs)[0]
                        result["full"] = chosen["url"]
                        notes.append(
                            f"full doc via detail page: {chosen['caption'][:50]}")
                    else:
                        notes.append(
                            f"detail page had NO pdf links: {filing['url'][:90]}")
                except Exception as exc:
                    notes.append(
                        f"detail page failed: {type(exc).__name__}: {str(exc)[:70]}")

    if not result["abridged"] and not result["full"]:
        notes.append("matched the company but found no PDF links")
    return result
