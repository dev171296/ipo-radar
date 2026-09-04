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


def download(url: str) -> bytes:
    """Fetch a PDF. SEBI serves these over an ordinary connection."""
    session = plain_session()
    resp = session.get(url, timeout=90)
    if resp.status_code != 200:
        raise FetchError(f"PDF download failed: HTTP {resp.status_code}")

    size_mb = len(resp.content) / 1_000_000
    if size_mb > MAX_PDF_MB:
        raise FetchError(f"PDF is {size_mb:.0f} MB — refusing, that isn't a prospectus")
    if len(resp.content) < 20_000:
        raise FetchError(f"only {len(resp.content)} bytes — probably an error page")
    if not resp.content[:5].startswith(b"%PDF"):
        raise FetchError("that file is not a PDF")

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


def fetch_pages(url: str) -> list:
    """Download a prospectus and return its text, one entry per page."""
    return to_pages(download(url))


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
