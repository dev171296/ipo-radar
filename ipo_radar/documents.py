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
import time

import pypdfium2 as pdfium

from .http import FetchError, TIMEOUT, plain_session

MAX_PDF_MB = 80          # anything larger is almost certainly not a prospectus


DOWNLOAD_ATTEMPTS = 4


def download(url: str, referer: str = None) -> bytes:
    """
    Fetch a PDF, and keep going when the connection gives up halfway.

    A prospectus is 10-30 MB and SEBI's server drops the connection partway
    often enough that a single try is not good enough: measured 5 Sep 2026,
    two of four downloads died at 6.2 MB of 9.6 MB and 2.4 MB of 12.3 MB.

    So we stream the file, and if it breaks we ask for the REST of it — the
    `Range` header, the same mechanism your browser's "resume download" uses.
    If the server ignores that (answers 200 rather than 206) we simply start
    the file again. Four goes, then we give up and try on the next run.

    Some SEBI documents are only served to a visitor who looks like they came
    from the page displaying them, so when we know that page we visit it first
    and name it as the Referer — what a browser does. Harmless when not needed.
    """
    session = plain_session()
    base_headers = {}
    if referer:
        base_headers["Referer"] = referer
        try:
            session.get(referer, timeout=TIMEOUT)
        except Exception:
            pass

    body = b""
    last_problem = None

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        headers = dict(base_headers)
        if body:
            headers["Range"] = f"bytes={len(body)}-"

        try:
            resp = session.get(url, timeout=180, headers=headers, stream=True)

            if resp.status_code not in (200, 206):
                raise FetchError(f"HTTP {resp.status_code} for {url}")
            if body and resp.status_code == 200:
                body = b""            # server won't resume — start over

            if not body and resp.status_code == 200:
                first = next(resp.iter_content(chunk_size=8192), b"")
                if not first.startswith(b"%PDF"):
                    kind = resp.headers.get("content-type", "?")
                    opening = first[:160].decode("utf-8", "replace").replace("\n", " ")
                    raise FetchError(f"not a PDF | url={url} | type={kind} "
                                     f"| starts: {opening}")
                body += first

            for chunk in resp.iter_content(chunk_size=1 << 16):
                body += chunk
                if len(body) / 1_000_000 > MAX_PDF_MB:
                    raise FetchError(f"over {MAX_PDF_MB} MB — that isn't a prospectus")

            expected = resp.headers.get("content-range", "").rsplit("/", 1)[-1]
            if expected.isdigit() and len(body) < int(expected):
                raise IOError(f"short: {len(body)} of {expected} bytes")

            break                      # got the whole thing

        except FetchError:
            raise                      # a wrong file is not worth retrying
        except Exception as exc:
            last_problem = exc
            if attempt == DOWNLOAD_ATTEMPTS:
                raise FetchError(
                    f"gave up after {attempt} tries with {len(body):,} bytes "
                    f"of {url.rsplit('/', 1)[-1]}: {type(exc).__name__} {exc}")
            print(f"        download broke at {len(body):,} bytes — "
                  f"resuming (try {attempt + 1} of {DOWNLOAD_ATTEMPTS})")
            time.sleep(2 * attempt)

    if len(body) < 20_000:
        raise FetchError(f"only {len(body)} bytes — probably an error page")
    if not body.startswith(b"%PDF"):
        raise FetchError(f"not a PDF: {url}")

    return body


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
