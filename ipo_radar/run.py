"""
The entry point. One run of the whole pipeline.

Phase 1 scope: discover IPOs from NSE, record them, keep an append-only
history, and prove the price ladder works. No scoring, no AI, no email yet.

Design rule: one dead source must never stop the run. Every collector is
wrapped, and a failure is recorded as data rather than raised as a crash.
"""

import sys
import traceback

from . import storage
from .collectors import nse, prices
from .identity import find_match, make_id, normalise


def collect_ipos():
    """Fetch from NSE and fold the results into our store."""
    print("\n[1] NSE — IPO calendar")
    try:
        rows, field_names = nse.fetch()
    except Exception as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}")
        return 0, 0

    print(f"    got {len(rows)} rows")
    print(f"    fields NSE actually returns: {', '.join(field_names)}")

    existing = storage.all_records()
    new_count = updated_count = 0

    for row in rows:
        if not row.get("name"):
            continue

        match = find_match(existing, row)
        if match:
            ipo_id = match["id"]
            record = match
            updated_count += 1
        else:
            ipo_id = make_id(row["name"], row["dates"].get("open", ""))
            record = {"id": ipo_id, "aliases": [], "first_seen": storage.now()}
            existing.append(record)
            new_count += 1
            storage.append_event(ipo_id, "discovered",
                                 detail=row["name"], source="nse")
            print(f"    NEW  {ipo_id}  ({row['type']}, opens {row['dates'].get('open')})")

        # Keep every spelling we have ever seen, so other sources can be matched later.
        aliases = set(record.get("aliases", []))
        aliases.add(row["name"])
        record["aliases"] = sorted(aliases)
        record["normalised_name"] = normalise(row["name"])

        # The append-only truth: one line per observation, never overwritten.
        storage.append_history(ipo_id, {"kind": "nse_calendar", "data": row})

        # The derived view: regenerated from what we just learned.
        record.update({
            "name": row["name"],
            "symbol": row.get("symbol"),
            "type": row["type"],
            "status": row["status"],
            "dates": row["dates"],
            "price_band": row.get("price_band_text"),
            "issue_size_shares": row.get("issue_size_shares"),
            "also_on_bse": row.get("also_on_bse"),
            "sources": {"calendar": "nse", "fetched_at": storage.now()},
            "missing": [],
        })
        storage.rebuild_record(ipo_id, record)

    storage.rebuild_index(existing)
    return new_count, updated_count


def check_price_ladder():
    """
    Exercise the fallback ladder on a known stock.

    We have no listed IPOs to track yet, so this is a live self-test: it proves
    the ladder still works today and tells us which rung answered.
    """
    print("\n[2] Price ladder self-test (Reliance)")
    try:
        bar = prices.latest_price({"bse_code": "500325", "yahoo": "RELIANCE.NS"})
        print(f"    answered by: {bar['source']}   close={bar['close']}")
        for attempt in bar.get("attempts", []):
            print(f"      rung {attempt['rung']}: {attempt['result']}")
        return True
    except Exception as exc:
        print(f"    ALL RUNGS FAILED: {exc}")
        return False


def main():
    print("=" * 66)
    print("IPO RADAR — collector run")
    print("=" * 66)

    storage.ensure_dirs()

    try:
        new, updated = collect_ipos()
    except Exception:
        traceback.print_exc()
        new = updated = 0

    ladder_ok = check_price_ladder()

    print("\n" + "=" * 66)
    print(f"  {new} new IPOs, {updated} updated")
    print(f"  price ladder: {'ok' if ladder_ok else 'FAILED'}")
    print(f"  tracking {len(storage.all_records())} IPOs in total")
    print("=" * 66)

    # A collector run never fails the build. Missing data is recorded as
    # missing; it is not an error that should stop tomorrow's run.
    sys.exit(0)


if __name__ == "__main__":
    main()
