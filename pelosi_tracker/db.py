"""SQLite persistence."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

PARSER_VERSION = "0.1.0"
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    return conn


def _delay_days(traded: str | None, filed: str | None) -> int | None:
    if not traded or not filed:
        return None
    try:
        return (date.fromisoformat(filed) - date.fromisoformat(traded)).days
    except ValueError:
        return None


def upsert_filing(conn, pf, source_url: str | None, fetched_via: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO filings
           (filing_id, doc_type, filing_type, filer_name, state_district,
            filing_year, filing_date, signed_date, source_url, fetched_via,
            parser_version, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pf.filing_id, pf.doc_type, pf.meta.get("filing_type"),
         pf.meta.get("filer_name"), pf.meta.get("state_district"),
         pf.meta.get("filing_year"), pf.meta.get("filing_date"),
         pf.signed_date, source_url, fetched_via, PARSER_VERSION,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))


def upsert_assets(conn, registry: dict) -> None:
    for a in registry.values():
        conn.execute(
            "INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?)",
            (a["asset_id"], a.get("ticker"), a["canonical_name"],
             int(a["is_security"]), json.dumps(a["instruments"]),
             json.dumps(a["name_variants"])))


def insert_transactions(conn, txs: list[dict]) -> None:
    for t in txs:
        prov = t.get("provenance") or [{"filing_id": t.get("filing_id"),
                                        "schedule": t.get("source_schedule")}]
        tid = f"{prov[0]['filing_id']}:{t.get('row_order', 0)}:{t.get('asset_id')}:{t.get('transaction_date')}"
        conn.execute(
            """INSERT OR REPLACE INTO transactions VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, t.get("asset_id"), t.get("ticker"), t.get("asset_name"),
             t.get("asset_type_code"), t.get("instrument"), t.get("owner_code"),
             t.get("transaction_code"), t.get("transaction_date"),
             t.get("notification_date"),
             t["amount"].get("min"), t["amount"].get("max"), t["amount"].get("raw"),
             int(t["amount"].get("max_inferred", False)),
             t.get("filing_status"), t.get("description"), t.get("comments"),
             json.dumps(t.get("facts")),
             _delay_days(t.get("transaction_date"), t.get("filed_date")),
             int(t.get("corroborated", False)), json.dumps(prov),
             int(t.get("needs_review", False)),
             json.dumps(t.get("notes", []))))


def insert_positions(conn, positions: list[dict]) -> None:
    for i, p in enumerate(positions):
        pid = f"{p.get('filing_id')}:{i}"
        conn.execute(
            """INSERT OR REPLACE INTO positions VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, p.get("filing_id"), p.get("asset_id"), p.get("ticker"),
             p.get("asset_name"), p.get("asset_type_code"), p.get("instrument"),
             p.get("owner_code"),
             p["value"].get("min"), p["value"].get("max"), p["value"].get("raw"),
             p.get("income_types"),
             (p.get("income") or {}).get("raw"),
             p.get("description"), p.get("location"),
             json.dumps(p.get("facts")), int(p.get("needs_review", False)),
             json.dumps(p.get("notes", []))))


def replace_holdings(conn, holdings: dict) -> None:
    conn.execute("DELETE FROM holdings")
    for kind in ("stocks", "options", "other_assets"):
        for h in holdings.get(kind, []):
            conn.execute("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?)",
                         (h.get("asset_id"), kind.rstrip("s"),
                          json.dumps(h), holdings["as_of"]))
