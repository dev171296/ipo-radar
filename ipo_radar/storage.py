"""
Storage. Append-only by design.

Devanshu's rule, enforced in code rather than by convention: there is no
update function and no delete function in this file. You cannot call one
by accident because one does not exist.

Three kinds of file, three different jobs:

  history/<id>.jsonl   Append-only. One line per reading, forever.
                       This is the truth. Subscription velocity, GMP trend and
                       the metric charts are all computed from it. Overwriting
                       a value here would destroy data we can never recover.

  runlog/<id>.jsonl    Append-only. One line per event. Powers the process
                       monitor: what happened, when, from which source.

  ipos/<id>.json       DERIVED. Rebuilt from history each run. Safe to delete
                       and regenerate. Rewriting this is not editing history.

  index.json           DERIVED. One row per IPO so the dashboard opens one file
                       instead of three hundred.
"""

import json
import os
from datetime import datetime, timezone

DATA = "data"
DIRS = ["ipos", "history", "runlog", "predictions", "outcomes", "tracking", "corpus"]


def now() -> str:
    """One timestamp format everywhere: UTC, ISO, seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs():
    for d in DIRS:
        os.makedirs(os.path.join(DATA, d), exist_ok=True)


def _path(kind, name):
    return os.path.join(DATA, kind, name)


# ---------------------------------------------------------------- append-only
def append_history(ipo_id: str, reading: dict):
    """Add one observation. Never modifies what is already there."""
    reading = {"at": now(), **reading}
    with open(_path("history", f"{ipo_id}.jsonl"), "a") as f:
        f.write(json.dumps(reading, ensure_ascii=False) + "\n")


def append_event(ipo_id: str, event: str, detail=None, source=None, ok=True):
    """Record something that happened, for the process monitor."""
    entry = {"at": now(), "event": event, "ok": ok}
    if detail is not None:
        entry["detail"] = detail
    if source:
        entry["source"] = source
    with open(_path("runlog", f"{ipo_id}.jsonl"), "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def already_have_trading_day(ipo_id: str, kind: str, trading_day: str) -> bool:
    """
    Have we already recorded this exact trading day?

    We run four times a day but the price file only changes once a day. Without
    this check the same closing price would be written four times, and a flat
    day would look like four separate observations — which would quietly corrupt
    anything that measures how prices move.
    """
    for line in read_history(ipo_id):
        if line.get("kind") == kind and line.get("trading_day") == trading_day:
            return True
    return False


def read_history(ipo_id: str) -> list:
    path = _path("history", f"{ipo_id}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- derived
def rebuild_record(ipo_id: str, record: dict):
    """
    Write the current working view of an IPO.

    Named 'rebuild' rather than 'update' on purpose: this file is regenerated
    from history, never hand-edited. Losing it costs nothing.
    """
    record["id"] = ipo_id
    record["rebuilt_at"] = now()
    with open(_path("ipos", f"{ipo_id}.json"), "w") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def read_record(ipo_id: str):
    path = _path("ipos", f"{ipo_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def all_records() -> list:
    folder = os.path.join(DATA, "ipos")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json"):
            with open(os.path.join(folder, name)) as f:
                out.append(json.load(f))
    return out


def rebuild_index(records: list):
    """One small file the dashboard can read in a single request."""
    rows = [{
        "id": r["id"],
        "name": r.get("name"),
        "type": r.get("type"),
        "status": r.get("status"),
        "dates": r.get("dates", {}),
        "price_band": r.get("price_band"),
        "missing": r.get("missing", []),
    } for r in records]
    payload = {"generated_at": now(), "count": len(rows), "ipos": rows}
    with open(os.path.join(DATA, "index.json"), "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
