"""Persistence: reference data (vendor master, POs, rules), the invoice ledger, and run history (SQLite)."""
import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SAMPLES = ROOT / "samples"
RUNTIME = Path(os.getenv("RUNTIME_DIR", ROOT / "runtime"))
UPLOADS = RUNTIME / "uploads"
LEDGER = RUNTIME / "ledger.json"
DB = RUNTIME / "runs.db"

_lock = threading.RLock()


def _load(name: str):
    return json.loads((DATA / name).read_text())


COMPANY = _load("company.json")
VENDORS = _load("vendors.json")
POS = _load("purchase_orders.json")
RULES = _load("rules.json")


def init():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    if not LEDGER.exists():
        shutil.copy(DATA / "ledger_seed.json", LEDGER)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, filename TEXT, sha256 TEXT,
            status TEXT, decision TEXT, vendor_name TEXT, invoice_number TEXT, total REAL,
            engine TEXT, duration_ms INTEGER, primary_reason TEXT, result_json TEXT)""")


def _conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def reset():
    with _lock:
        if DB.exists():
            DB.unlink()
        shutil.rmtree(UPLOADS, ignore_errors=True)
        if LEDGER.exists():
            LEDGER.unlink()
        init()


# ------------------------------------------------------------------ ledger
def ledger() -> list[dict]:
    with _lock:
        return json.loads(LEDGER.read_text())


def add_to_ledger(entry: dict):
    with _lock:
        rows = ledger()
        entry["ledger_id"] = f"L-{9001 + len(rows)}"
        rows.append(entry)
        LEDGER.write_text(json.dumps(rows, indent=2))


def po_consumption(po_number: str) -> dict:
    """Quantity and value already billed against each PO line (paid or approved invoices)."""
    used = {}
    for e in ledger():
        if e.get("po_number") != po_number:
            continue
        for ln in e.get("lines", []):
            u = used.setdefault(ln["po_line"], {"quantity": 0.0, "amount": 0.0, "invoices": []})
            u["quantity"] += ln["quantity"]
            u["amount"] += ln["amount"]
            u["invoices"].append(e["invoice_number"])
    return used


# ------------------------------------------------------------------ runs
def create_run(filename: str, sha: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("INSERT INTO runs (created_at, filename, sha256, status) VALUES (?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), filename, sha, "running"))
        return cur.lastrowid


def finish_run(run_id: int, result: dict):
    d = result.get("decision", {})
    ex = result.get("extracted") or {}
    with _lock, _conn() as c:
        c.execute("""UPDATE runs SET status=?, decision=?, vendor_name=?, invoice_number=?, total=?, engine=?,
                     duration_ms=?, primary_reason=?, result_json=? WHERE id=?""",
                  ("done", d.get("outcome"), (result.get("vendor") or {}).get("name") or ex.get("vendor_name"),
                   ex.get("invoice_number"), ex.get("total"), result.get("engine"), result.get("duration_ms"),
                   d.get("headline"), json.dumps(result), run_id))


def fail_run(run_id: int, error: str):
    with _lock, _conn() as c:
        c.execute("UPDATE runs SET status=?, decision=?, primary_reason=? WHERE id=?", ("error", "ERROR", error, run_id))


def list_runs() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id, created_at, filename, status, decision, vendor_name, invoice_number, total, engine, "
                         "duration_ms, primary_reason FROM runs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json") or "null")
        return d


def prior_runs(exclude_id: int) -> list[dict]:
    """Finished runs other than this one, with their extracted identity (for duplicate checks)."""
    out = []
    with _conn() as c:
        for r in c.execute("SELECT id, sha256, decision, result_json FROM runs WHERE id != ? AND status='done'", (exclude_id,)):
            res = json.loads(r["result_json"] or "{}")
            out.append({"run_id": r["id"], "sha256": r["sha256"], "decision": r["decision"],
                        "vendor_id": (res.get("vendor") or {}).get("vendor_id"),
                        "extracted": res.get("extracted") or {}})
    return out
