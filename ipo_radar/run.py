"""
The entry point. One run of the whole pipeline.

Phase 1 scope: discover IPOs from NSE, record them, keep an append-only
history, and prove the price ladder works. No scoring, no AI, no email yet.

Design rule: one dead source must never stop the run. Every collector is
wrapped, and a failure is recorded as data rather than raised as a crash.
"""

import json
import os
import sys
import traceback

from . import (abridged, analysts, call, documents, evidence, grading, publisher,
               scoring, sections, storage)
from .collectors import nse, prices, sebi
from .identity import find_match, make_id, normalise


DASHBOARD_URL = "https://dev171296.github.io/ipo-radar/"


def _read_json(*parts):
    path = os.path.join(*parts)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None




def _write_json(payload, *parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    return path


# How long the AI step may keep STARTING new IPOs. GitHub stops any run at six
# hours, and everything gathered is saved only at the end of a run — so a run
# that simply kept going would lose all of its own work. Four hours leaves room
# for the email, the dashboard files and the save. Anything not reached is
# picked up by the next run; finished IPOs are never re-asked.
ANALYST_BUDGET_MINUTES = analysts.ANALYST_BUDGET_MINUTES   # set by ANALYST_MODE


def _analysis_order(record):
    """
    Most urgent first, so a run cut short by the time allowance has spent its
    time where it matters: issues still open and closing soonest, then issues
    yet to open, then issues that have already closed.
    """
    from datetime import date
    close = (record.get("dates") or {}).get("close") or "9999-12-31"
    today = date.today().isoformat()
    finished = 1 if close < today else 0
    return (finished, close if not finished else "", record.get("name", ""))


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


def build_evidence():
    """
    Assemble one evidence bundle per IPO — everything we know, in one file,
    every number carrying the document and page it came from. This is what the
    scorer and (later) both AI models will read. Nothing else.
    """
    print("\n[3] Evidence bundles and quant scores")
    built = 0
    for record in storage.all_records():
        try:
            bundle = evidence.build(record["id"])
            evidence.save(bundle)
            result = scoring.verdict(bundle)
            scoring.save(result)
            built += 1
            print(f"    {record.get('name')}")
            print(f"      {evidence.summarise(bundle)}")
            print(f"      SCORE  {scoring.summarise(result)}")
            for part in result["fundamentals"]["components"]:
                print(f"        F {part['component']}: {part['points']}/"
                      f"{part['weight']} — {part['basis']}")
            for part in result["demand"]["components"]:
                print(f"        D {part['component']}: {part['points']}/"
                      f"{part['weight']} — {part['basis']}")
            for item in result["vetoes"]["triggered"]:
                print(f"        VETO: {item['veto']}")
            for item in result["flags"]:
                print(f"        flag: {item['flag']} — {item['detail']}")
            for gap in bundle["missing"][:3]:
                print(f"      missing: {gap['what']} — {gap['why']}")
            if len(bundle["missing"]) > 3:
                print(f"      ...and {len(bundle['missing']) - 3} more gap(s)")
        except Exception as exc:
            print(f"    {record.get('name')}: could not build — "
                  f"{type(exc).__name__} {str(exc)[:90]}")
    return built


def run_analysts():
    """
    The two AI analysts, on the evidence we have gathered.

    Runs only where there is something to read and something new to say: an IPO
    with a prospectus on file, whose evidence has changed since the last answer.
    Both models get an identical brief and never see each other.
    """
    print("\n[4] AI analysts")
    if not any(os.environ.get(analysts.KEY_NAMES[n]) for n in analysts.ANALYSTS):
        print("    no AI keys set — skipping (the quant scores stand on their own)")
        return 0

    print(f"    analysts: {', '.join(analysts.ANALYSTS)}; both read everything")
    # A one-line health check per key — its length and shape, never its value —
    # so a rejected key can be diagnosed from the log alone.
    for name in analysts.ANALYSTS:
        print(f"    key check: "
              f"{analysts.key_problem(name, os.environ.get(analysts.KEY_NAMES[name]))}")
    print(f"    mode: {analysts.ANALYST_MODE} — Kimi effort "
          f"{analysts.NVIDIA_REASONING_EFFORT}, up to "
          f"{analysts.NVIDIA_MAX_SECONDS // 60} min per answer, "
          f"{ANALYST_BUDGET_MINUTES} min for the whole AI step")

    import time
    started = time.monotonic()
    done, deferred = 0, []
    for record in sorted(storage.all_records(), key=_analysis_order):
        ipo_id = record["id"]
        spent = (time.monotonic() - started) / 60
        if spent > ANALYST_BUDGET_MINUTES:
            deferred.append(record.get("name"))
            continue
        path = os.path.join(storage.DATA, "docs", ipo_id, "full.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            bundle = evidence.build(ipo_id)
            score = scoring.verdict(bundle)
            results = analysts.analyse(bundle, document.get("sections") or {},
                                       score)
            comparison = analysts.compare(results)
            analysts.save(ipo_id, results, comparison)
            done += 1

            print(f"    {record.get('name')}")
            print(f"      {analysts.summarise(results, comparison)}")
            for name, result in results.items():
                answer = (result or {}).get("answer")
                if not answer:
                    continue
                for view in ("listing_view", "longterm_view"):
                    body = answer.get(view) or {}
                    print(f"        {name} {view}: {body.get('score')} — "
                          f"{str(body.get('reasoning'))[:150]}")
                checked = answer.get("citation_check") or {}
                print(f"        {name} citations: "
                      f"{checked.get('citations_verified')}/"
                      f"{checked.get('claims_checked')} verified"
                      + (f", {checked['citations_not_matching_what_we_sent']} "
                         f"NOT in what we sent"
                         if checked.get("citations_not_matching_what_we_sent")
                         else ""))
            skipped = (results.get("nvidia") or {}).get("skipped")
            if skipped:
                print(f"        second analyst: not asked — {skipped}")
            for gap in comparison.get("disagreements", []):
                print(f"        DISAGREEMENT on {gap['on']}: {gap['scores']} "
                      f"({gap['gap']} apart)")
        except Exception as exc:
            print(f"    {record.get('name')}: analyst failed — "
                  f"{type(exc).__name__} {str(exc)[:120]}")
        finally:
            if analysts.LAST_NVIDIA_NOTES:
                for note in analysts.LAST_NVIDIA_NOTES:
                    print(f"        nvidia: {note}")
                analysts.LAST_NVIDIA_NOTES[:] = []

    if deferred:
        print(f"    time allowance used — {len(deferred)} IPO(s) left for the next "
              f"run, which will pick them up where this one stopped: "
              f"{', '.join(deferred[:8])}{' …' if len(deferred) > 8 else ''}")
    return done


def grade_predictions():
    """
    Check the calls we made against what the market then did.

    Only IPOs that have listed can be graded, and only answers written before
    trading began on listing day count. See grading.py for the rules.
    """
    print("\n[4b] Track record — calls against what actually happened")
    board = grading.run()
    print(f"    {board['listed_ipos']} IPO(s) have listed so far"
          + (f"; average listing gain {board['average_listing_gain_pct']}%, "
             f"and saying 'apply' to every one would have been right "
             f"{board['base_rate_pct']}% of the time"
             if board['listed_ipos'] else ""))
    for name, row in board["predictors"].items():
        print(f"      {name}: {row['listing_right']} of {row['listing_graded']} "
              f"listing calls right"
              + (f" ({row['listing_hit_rate_pct']}%)" if row.get('listing_hit_rate_pct') is not None else "")
              + f"; no view on {row['no_view']}")
    return board


def publish():
    """
    Build the digest and send it, if we have somewhere to send it.

    The digest is written to data/email/latest.html on every run whether or not
    it goes anywhere, so the formatting can be opened in a browser and checked
    without spending an email.
    """
    print("\n[5] Digest")
    companies = []
    for record in storage.all_records():
        ipo_id = record["id"]
        comparison = _read_json(storage.DATA, "analysis", ipo_id,
                                "comparison-latest.json")
        score = _read_json(storage.DATA, "scores", ipo_id, "latest.json")
        decision = call.decide(score, comparison)
        _write_json(decision, storage.DATA, "calls", f"{ipo_id}.json")

        companies.append({
            "ipo": record,
            "call": decision,
            "score": score,
            "bundle": _read_json(storage.DATA, "evidence", ipo_id, "latest.json"),
            "ai": {
                "groq": _read_json(storage.DATA, "analysis", ipo_id,
                                   "groq-latest.json"),
                "nvidia": _read_json(storage.DATA, "analysis", ipo_id,
                                     "nvidia-latest.json"),
                "comparison": comparison,
            },
        })

    for entry in companies:
        listing = (entry["call"]["listing"] or {})
        longterm = (entry["call"]["longterm"] or {})
        print(f"    {entry['ipo'].get('name')}: applying -> {listing['call']}"
              f" ({listing.get('confidence')}), holding -> {longterm['call']}"
              f" ({longterm.get('confidence')})")

    subject, body, text = publisher.build(
        companies, DASHBOARD_URL,
        scoreboard=_read_json(storage.DATA, "grades", "scoreboard.json"))
    print(f"    subject: {subject}")
    path = publisher.save(subject, body, text)
    print(f"    written to {path} ({len(body):,} bytes)")

    outcome = publisher.send(subject, body, text)
    if outcome.get("sent"):
        print(f"    emailed to {outcome['to']}")
    else:
        print(f"    not emailed — {outcome.get('why')}")
    return outcome.get("sent", False)


def check_price_ladder():
    """
    Exercise the fallback ladder on a known stock.

    We have no listed IPOs to track yet, so this is a live self-test: it proves
    the ladder still works today and tells us which rung answered.
    """
    print("\n[6] Price ladder self-test (Reliance)")
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

    try:
        bundles = build_evidence()
    except Exception:
        traceback.print_exc()
        bundles = 0

    try:
        analysed = run_analysts()
    except Exception:
        traceback.print_exc()
        analysed = 0

    try:
        grade_predictions()
    except Exception:
        traceback.print_exc()

    try:
        emailed = publish()
    except Exception:
        traceback.print_exc()
        emailed = False

    ladder_ok = check_price_ladder()

    print("\n" + "=" * 66)
    print(f"  {new} new IPOs, {updated} updated")
    print(f"  {docs_done} prospectus documents read")
    print(f"  {bundles} evidence bundles built and scored")
    print(f"  {analysed} IPOs sent to the AI analysts")
    print(f"  digest: {'emailed' if emailed else 'written to disk only'}")
    print(f"  price ladder: {'ok' if ladder_ok else 'FAILED'}")
    print(f"  tracking {len(storage.all_records())} IPOs in total")
    print("=" * 66)

    # A collector run never fails the build. Missing data is recorded as
    # missing; it is not an error that should stop tomorrow's run.
    sys.exit(0)


if __name__ == "__main__":
    main()
