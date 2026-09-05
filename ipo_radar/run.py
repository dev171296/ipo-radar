"""
The entry point. One run of the whole pipeline.

Phase 1 scope: discover IPOs from NSE, record them, keep an append-only
history, and prove the price ladder works. No scoring, no AI, no email yet.

Design rule: one dead source must never stop the run. Every collector is
wrapped, and a failure is recorded as data rather than raised as a crash.
"""

import sys
import traceback

from . import abridged, documents, sections, storage
from .collectors import nse, prices, sebi
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
            print(f"    NEW  {ipo_id}  ({row['type']}, "
                  f"{row['dates'].get('open')} to {row['dates'].get('close')})")

        # Keep every spelling we have ever seen, so other sources can be matched later.
        aliases = set(record.get("aliases", []))
        aliases.add(row["name"])
        record["aliases"] = sorted(aliases)
        record["normalised_name"] = normalise(row["name"])

        # The append-only truth: one line per observation, never overwritten.
        storage.append_history(ipo_id, {"kind": "nse_calendar", "data": row})

        sub = (row.get("subscription") or {}).get("times")
        if sub is not None:
            print(f"         subscribed {sub}x")

        # The derived view: regenerated from what we just learned.
        record.update({
            "name": row["name"],
            "symbol": row.get("symbol"),
            "type": row["type"],
            "status": row["status"],
            "dates": row["dates"],
            "price_band": row.get("price_band_text"),
            "issue_size_shares": row.get("issue_size_shares"),
            "subscription": row.get("subscription"),
            "also_on_bse": row.get("also_on_bse"),
            "sources": {"calendar": "nse", "fetched_at": storage.now()},
            "missing": [],
        })
        storage.rebuild_record(ipo_id, record)

    storage.rebuild_index(existing)
    return new_count, updated_count


def collect_prospectuses():
    """
    Find and read each IPO's prospectus.

    Two documents per company:
      abridged  ~30 pages, the regulator's mandated summary. Fast, reliable.
      full      400-600 pages. Where litigation and related-party detail live.

    A prospectus never changes once filed, so we do this once per IPO and skip
    it forever after.
    """
    print("\n[2] SEBI — prospectuses")

    records = storage.all_records()
    todo = [r for r in records
            if not (storage.has_sections(r["id"], "abridged")
                    and storage.has_sections(r["id"], "full"))]
    # Documents we already read are skipped. Delete data/docs/<id>/ to redo one.
    if not todo:
        print("    all prospectuses already fetched")
        return 0

    try:
        filings = sebi.list_filings("rhp")
        print(f"    SEBI currently lists {len(filings)} documents")
    except Exception as exc:
        print(f"    could not read SEBI's listing: {type(exc).__name__}: {exc}")
        return 0

    done = 0
    for record in todo:
        ipo_id, name = record["id"], record.get("name", "")
        print(f"\n    {name}")

        found = sebi.documents_for(name, filings)
        for note in found.get("notes", []):
            print(f"      note: {note}")
        if not found["abridged"] and not found["full"]:
            print("      no documents found — will try again next run")
            storage.append_event(ipo_id, "prospectus_not_found", source="sebi", ok=False)
            continue
        print(f"      matched as: {found['matched_as']}")

        # Download every PDF the detail page offers and decide what each one
        # IS by its length, rather than trusting its name. The summary form runs
        # 9-16 pages; the prospectus runs ~500.
        candidates = found.get("candidates") or []
        if found.get("abridged"):
            candidates = [{"url": found["abridged"], "how": "listing"}] + candidates

        seen_urls = set()
        for candidate in candidates:
            url = candidate["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                pages = documents.fetch_pages(
                    url, referer=candidate.get("source_page"))
            except Exception as exc:
                print(f"      skipped {url.rsplit('/', 1)[-1][:44]}: "
                      f"{type(exc).__name__} {str(exc)[:300]}")
                continue

            which = documents.classify(pages)
            if storage.has_sections(ipo_id, which):
                print(f"      already have the {which} document")
                continue

            meta = {"url": url, "total_pages": len(pages), "found_via": candidate["how"]}

            if which == "abridged":
                extracted = abridged.read(pages)
                financials_page = (extracted.get("financials") or {}).get("page")
                ratios = abridged.read_ratios(pages, prefer_page=financials_page)
                meta["keep_pages"] = pages
                if ratios:
                    meta["ratios"] = ratios
                print(f"      SUMMARY FORM ({len(pages)} pages) -> "
                      f"{len(extracted)} fields")
                if extracted:
                    print(f"        {abridged.summarise(extracted)}")
                for name, body in sorted(ratios.items()):
                    print(f"        {name}: {body['years']}")
                if not ratios:
                    print("        no financial ratios matched")
            else:
                extracted = sections.split(pages)
                print(f"      FULL PROSPECTUS ({len(pages)} pages) -> "
                      f"{len(extracted)} sections")
                if extracted:
                    print(f"        {sections.summarise(extracted)}")
                else:
                    shape = sections.describe(pages)
                    meta["structure_seen"] = shape
                    print(f"        {shape['heading_count']} heading-like lines, "
                          f"sample:")
                    for line in shape["heading_candidates"][:12]:
                        print(f"          {line}")

            try:
                storage.save_sections(ipo_id, which, extracted, meta)
                storage.append_event(
                    ipo_id, f"{which}_read",
                    detail=f"{len(pages)} pages, {len(extracted)} parts",
                    source="sebi")
                done += 1
            except Exception as exc:
                # One awkward document must never abandon the other four
                # companies. Record it and carry on.
                print(f"      could not save: {type(exc).__name__}: {str(exc)[:90]}")
                storage.append_event(ipo_id, f"{which}_save_failed",
                                     detail=str(exc)[:200], source="sebi", ok=False)

    return done


def check_price_ladder():
    """
    Exercise the fallback ladder on a known stock.

    We have no listed IPOs to track yet, so this is a live self-test: it proves
    the ladder still works today and tells us which rung answered.
    """
    print("\n[3] Price ladder self-test (Reliance)")
    from .collectors import bhavcopy
    print(f"    India time now: {bhavcopy.india_now():%Y-%m-%d %H:%M} IST")
    print(f"    today's file expected yet? "
          f"{'yes' if bhavcopy.todays_file_should_exist() else 'no — before 7pm IST'}")
    print(f"    trading day we should be able to get: {bhavcopy.expected_day()}")
    try:
        bar = prices.latest_price({"nse": "RELIANCE",
                                   "bse_code": "500325",
                                   "yahoo": "RELIANCE.NS"})
        print(f"    answered by: {bar['source']}   close={bar['close']}"
              f"   volume={bar.get('volume')}   delivery%={bar.get('delivery_pct')}")
        if bar.get("date"):
            print(f"    price is for trading day: {bar['date']}")
        if bar.get("staleness"):
            print(f"    freshness: {bar['staleness']['note']}")
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

    try:
        docs_done = collect_prospectuses()
    except Exception:
        traceback.print_exc()
        docs_done = 0

    ladder_ok = check_price_ladder()

    print("\n" + "=" * 66)
    print(f"  {new} new IPOs, {updated} updated")
    print(f"  {docs_done} prospectus documents read")
    print(f"  price ladder: {'ok' if ladder_ok else 'FAILED'}")
    print(f"  tracking {len(storage.all_records())} IPOs in total")
    print("=" * 66)

    # A collector run never fails the build. Missing data is recorded as
    # missing; it is not an error that should stop tomorrow's run.
    sys.exit(0)


if __name__ == "__main__":
    main()
