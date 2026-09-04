"""
The fetching layer. Every network request in the system goes through here.

Why one place: when a site changes how it defends itself, we fix it once.

What probe 2 taught us, encoded here:
  - NSE and Yahoo need a real Chrome TLS fingerprint (curl_cffi), not just
    browser-looking headers. Plain `requests` is refused.
  - NSE needs a visit to its homepage first, to be given a session cookie.
  - WARP / Cloudflare proxying makes NSE WORSE (200 -> 403). No proxy anywhere.
  - curl_cffi must never be called from threads. This runs single-threaded.
"""

import time

import requests
from curl_cffi import requests as cffi

TIMEOUT = 25
POLITE_GAP = 1.0          # seconds between requests to the same host
MAX_RETRIES = 3

PLAIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(Exception):
    """Raised when a source could not be reached or gave us nothing usable."""


def plain_session():
    """An ordinary session. Enough for BSE, SEBI and Google News."""
    s = requests.Session()
    s.headers.update(PLAIN_HEADERS)
    return s


def chrome_session():
    """
    A session that copies Chrome's TLS handshake.

    Sites like NSE fingerprint the handshake itself, before any HTTP happens,
    so headers alone don't help. This is what makes NSE reachable at all.
    """
    return cffi.Session(impersonate="chrome")


def nse_session():
    """
    A Chrome session that has already earned NSE's session cookie.

    NSE refuses data requests from a cold start. Visiting the homepage first
    gets us the cookie; the session then carries it automatically.
    """
    s = chrome_session()
    home = s.get("https://www.nseindia.com", timeout=TIMEOUT)
    if home.status_code != 200:
        raise FetchError(f"NSE homepage refused us: HTTP {home.status_code}")
    if len(s.cookies) == 0:
        raise FetchError("NSE homepage gave no cookies")
    time.sleep(POLITE_GAP)
    return s


def get_json(session, url, referer=None, what="data"):
    """
    Fetch and return parsed JSON, with retries.

    Deliberately strict: a reply is only accepted if it is really JSON.
    Probe 1 taught us that a 200 can carry an HTML error page, and treating
    that as success is how bad data gets into a database quietly.
    """
    headers = {"Referer": referer} if referer else None
    last = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=TIMEOUT)
            if resp.status_code != 200:
                last = f"HTTP {resp.status_code}"
            else:
                try:
                    return resp.json()
                except Exception:
                    last = f"200 but not JSON ({len(resp.text)} bytes)"
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:120]}"

        if attempt < MAX_RETRIES:
            time.sleep(POLITE_GAP * attempt * 2)   # back off, don't hammer

    raise FetchError(f"{what} from {url} failed after {MAX_RETRIES} tries: {last}")
