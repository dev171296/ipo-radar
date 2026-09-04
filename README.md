# IPO Radar

Automated analysis of Indian IPOs (NSE + BSE, mainboard and SME).
Collects the numbers and the prospectus, scores them on published rules, has two
AI models judge them independently, and grades its own predictions against what
actually happened.

Runs entirely on free tiers. Personal research tool — **not investment advice**.
Grey market premium is an unofficial, unregulated indicator.

## Status — Phase 1: collector and storage

Working: NSE IPO calendar, price fallback ladder, append-only storage.
Not built yet: prospectus pipeline, retrieval, scoring, AI, dashboard, email.

## How data is fetched

Measured from a GitHub Actions runner, 4 Sep 2026:

| Source | Method | Note |
|---|---|---|
| NSE IPO calendar | `curl_cffi` Chrome impersonation, homepage first for a cookie | plain requests get 403 |
| BSE quotes | plain `requests` | works first try |
| Yahoo chart API | `curl_cffi` Chrome impersonation | called directly, never via `yfinance` |
| SEBI, Google News | plain `requests` | |

**No proxy.** Cloudflare WARP was tested and made NSE *worse* (200 → 403).

## Layout

    ipo_radar/
      http.py            all network access; where site defences are handled
      identity.py        deciding when two records are the same IPO
      storage.py         append-only store (no update or delete functions exist)
      collectors/
        nse.py           IPO calendar
        prices.py        BSE -> Yahoo fallback ladder
      run.py             one run of the pipeline
    data/                the store (written by the workflow, committed here)
    probe.py probe2.py   diagnostics, kept for when a source breaks

## Data rules

- `history/*.jsonl` is append-only and is the truth. Never overwritten.
- `ipos/*.json` and `index.json` are derived and can be deleted and rebuilt.
- Every value records which source produced it and when.
