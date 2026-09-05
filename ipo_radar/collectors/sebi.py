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


def _kind_of(link_text: str, url: str = "") -> str:
    """
    What sort of document is this link?

    Judged by the WEB ADDRESS, not the caption. SEBI nests the abridged link
    inside the RHP link on their listing page, so reading the caption of the
    RHP link returns both captions stuck together — and the word "abridged"
    in the second one made us misfile the first. Addresses do not nest.
    """
    address = (url or "").lower()
    caption = (link_text or "").lower()

    if address.endswith(".pdf"):
        # Measured 4 Sep 2026: every PDF linked directly from SEBI's listing
        # page turned out to be the abridged form — the ~15-page document whose
        # page 3 reads "IN THE NATURE OF ABRIDGED PROSPECTUS". The full 500-page
        # prospectus is not linked here. So a direct PDF is the abridged one
        # unless it says otherwise.
        if "drhp" in address or "draft" in address:
            return "drhp"
        return "abridged"

    if "/filings/public-issues/" in address:
        # A SEBI detail page. Which document it belongs to is in the caption,
        # but only the part BEFORE any nested link matters, so take the start.
        head = caption.split(" - ")[1] if " - " in caption else caption
        if head.strip().startswith("drhp") or "draft" in head[:30]:
            return "drhp_page"
        return "rhp_page"

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
        url = urljoin(BASE, anchor["href"])
        filings.append({
            "company": company,
            "normalised": normalise(company),
            "kind": _kind_of(caption, url),
            "url": url,
            "caption": caption,
        })
    return filings


def pdfs_on_page(page_url: str) -> list:
    """
    Every PDF referenced anywhere on a SEBI detail page.

    Measured 4 Sep 2026: the 508-page prospectus is DISPLAYED on that page in a
    document viewer rather than linked, so looking only at clickable links finds
    the 16-page summary and misses the real thing. We search the whole page
    source instead — links, embedded viewers, scripts, all of it.
    """
    session = plain_session()
    resp = session.get(page_url, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise FetchError(f"SEBI detail page: HTTP {resp.status_code}")

    html = resp.text
    seen, found = set(), []

    # 1. ordinary links
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        if ".pdf" in anchor["href"].lower():
            url = urljoin(page_url, anchor["href"])
            if url not in seen:
                seen.add(url)
                found.append({"url": url, "how": "link",
                              "caption": " ".join(anchor.get_text().split())[:100]})

    # 2. embedded viewers
    for tag, attribute in (("iframe", "src"), ("embed", "src"), ("object", "data")):
        for element in soup.find_all(tag):
            value = element.get(attribute, "")
            if ".pdf" in value.lower():
                url = urljoin(page_url, value)
                if url not in seen:
                    seen.add(url)
                    found.append({"url": url, "how": tag, "caption": ""})

    # 3. anything else in the page source — a viewer often takes its document
    #    from a script, where no tag search will find it.
    for match in re.findall(r"""[\"'\(]([^\"'\(\)\s]+\.pdf)""", html, re.I):
        url = urljoin(page_url, match)
        if url not in seen:
            seen.add(url)
            found.append({"url": url, "how": "source", "caption": ""})

    return found


def detail_page_for(company_name: str, filings: list = None):
    """The SEBI detail page for a company, if it has one."""
    filings = filings if filings is not None else list_filings("rhp")
    wanted = normalise(company_name)
    for filing in filings:
        if filing["normalised"] == wanted and filing["kind"] == "rhp_page":
            return filing["url"]
    return None


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
        if filing["kind"] == "abridged":
            result["abridged"] = filing["url"]
        elif filing["kind"] in ("rhp_page", "pdf_other"):
            if filing["url"].lower().endswith(".pdf"):
                result["full"] = filing["url"]
            else:
                # A detail page: follow it to find the document itself.
                try:
                    pdfs = pdfs_on_page(filing["url"])
                    result["candidates"] = pdfs
                    notes.append(f"detail page offers {len(pdfs)} PDF(s): "
                                 + ", ".join(f"{p['how']}:{p['url'].rsplit('/', 1)[-1][:40]}"
                                             for p in pdfs[:4]))
                    # Only take a link that is NOT the abridged form — we
                    # already have that, and storing it twice under two names
                    # is how the last version fooled itself.
                    main = [p for p in pdfs
                            if "abridged" not in p["caption"].lower()
                            and "abridged" not in p["url"].lower()]
                    if main:
                        result["full"] = main[0]["url"]
                        notes.append(f"full doc: {main[0]['caption'][:50]}")
                    elif pdfs:
                        notes.append(
                            "detail page only offers the abridged form — the "
                            "full prospectus is not linked here")
                    else:
                        notes.append(
                            f"detail page had NO pdf links: {filing['url'][:90]}")
                except Exception as exc:
                    notes.append(
                        f"detail page failed: {type(exc).__name__}: {str(exc)[:70]}")

    if not result["abridged"] and not result["full"]:
        notes.append("matched the company but found no PDF links")
    return result
