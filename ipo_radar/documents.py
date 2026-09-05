"""
Downloading prospectuses and getting the words out of them.

Two speeds, on purpose:

  pypdfium2   fast, plain text. A 600-page prospectus in seconds. This is what
              we use for everything by default.
  pdfplumber  slow but careful, understands table layout. Held in reserve for
              the peer-comparison and financial tables, where the arrangement
              of numbers on the page carries meaning.

We never keep the PDF itself. A full prospectus is 20-50 MB and three hundred
a year would bury the repository. We take the text we need and discard the rest.
"""

import io

import pypdfium2 as pdfium

from .http import FetchError, TIMEOUT, plain_session

MAX_PDF_MB = 80          # anything larger is almost certainly not a prospectus


def download(url: str, referer: str = None) -> bytes:
    """
    Fetch a PDF.

    Some SEBI documents are only served to a visitor who looks like they came
    from the page that displays them, so when we know that page we visit it
    first (which sets a cookie) and then name it as the Referer — exactly what
    a browser does. Harmless when it isn't needed.

    When the answer isn't a PDF we say what it actually was. Guessing at a
    silent failure twice is enough.
    """
    session = plain_session()
    headers = {}
    if referer:
        headers["Referer"] = referer
        try:
            session.get(referer, timeout=TIMEOUT)
        except Exception:
            pass

    resp = session.get(url, timeout=90, headers=headers)
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} for {url}")

    size_mb = len(resp.content) / 1_000_000
    if size_mb > MAX_PDF_MB:
        raise FetchError(f"PDF is {size_mb:.0f} MB — refusing, that isn't a prospectus")

    if not resp.content[:5].startswith(b"%PDF"):
        kind = resp.headers.get("content-type", "?")
        opening = resp.content[:160].decode("utf-8", "replace").replace("\n", " ")
        raise FetchError(
            f"not a PDF | url={url} | type={kind} | {len(resp.content)} bytes "
            f"| starts: {opening}")

    if len(resp.content) < 20_000:
        raise FetchError(f"only {len(resp.content)} bytes — probably an error page")

    return resp.content


def to_pages(pdf_bytes: bytes) -> list:
    """
    Turn a PDF into a list of strings, one per page.

    Keeping pages separate matters: it lets us cite 'page 214' later, which is
    what makes an AI's claim checkable rather than something we have to trust.
    """
    pages = []
    document = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    try:
        for page in document:
            text_page = page.get_textpage()
            pages.append(text_page.get_text_range() or "")
            text_page.close()
            page.close()
    finally:
        document.close()
    return pages


def fetch_pages(url: str, referer: str = None) -> list:
    """Download a prospectus and return its text, one entry per page."""
    return to_pages(download(url, referer=referer))


# Where the line falls between the two documents. Measured: the summary form
# runs 9-16 pages, the prospectus 508. Anything in between would be unusual.
LONG_DOCUMENT_PAGES = 60


def classify(pages: list) -> str:
    """
    Which document is this — the summary form or the full prospectus?

    Decided by LENGTH, not by filename. SEBI names the summary "- AP_p.pdf"
    with no word we could match on, and the prospectus is not named in a link
    at all. Page count cannot be misspelled.
    """
    return "full" if len(pages) >= LONG_DOCUMENT_PAGES else "abridged"
