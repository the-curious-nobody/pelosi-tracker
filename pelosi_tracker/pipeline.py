"""Production pipeline: from filings (fixture or live-synced) to the dataset.

Two entrypoints share one core:

  build_dataset(filings, out_dir, ...)   the parse -> merge -> reconstruct ->
                                         persist path used by run_demo.py,
                                         the live sync, and the tests.

  python3 -m pelosi_tracker.pipeline --data data/ --out out/ --years 2025 2026
                                         the LIVE path: diff the Clerk's index,
                                         download + extract new PDFs, rebuild.

Live-mode honesty guarantees:
  * known_gaps is computed, not asserted: any filing the index lists that this
    dataset could not parse (paper filings pending OCR, extraction failures)
    is declared as a gap and surfaces in staleness notes and on the site.
  * --fail-on-review exits non-zero if any row needs human review, so a CI
    deploy publishes nothing rather than publishing something wrong
    (accuracy over speed).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import assets as assets_mod
from . import db, ingest
from .filings import parse_filing_text
from .holdings import annotate_disclosure_delay, dedupe_transactions, reconstruct


@dataclass
class FilingInput:
    text: str
    source_url: str
    doc_id: str | None = None          # authoritative DocID from the index
    filing_date: str | None = None     # index FilingDate (ISO or M/D/YYYY)


def build_dataset(filings: list[FilingInput], out_dir: Path,
                  as_of: date | None = None,
                  known_gaps: list[str] | None = None) -> dict:
    """Parse every filing, merge, reconstruct holdings, persist. Returns a
    summary dict (counts + needs_review) for callers to act on."""
    out_dir.mkdir(parents=True, exist_ok=True)
    as_of = as_of or date.today()
    all_tx, all_pos, parsed = [], [], []
    review_detail: list[dict] = []

    for f in filings:
        pf = parse_filing_text(f.text, source=f.source_url)
        if f.doc_id:
            if pf.filing_id and pf.filing_id != f.doc_id:
                # The index is authoritative; a mismatch means mis-extraction.
                pf.filing_ids.insert(0, f.doc_id)
            elif not pf.filing_id:
                pf.filing_ids.append(f.doc_id)
        parsed.append((pf, f.source_url))
        filed = pf.signed_date or pf.meta.get("filing_date") or f.filing_date
        for i, r in enumerate(pf.rows):
            if not r.parsed:
                review_detail.append({
                    "filing_id": pf.filing_id, "kind": "unparsed_row",
                    "section": r.section, "notes": r.notes, "raw": r.text})
                continue
            rec = r.parsed
            rec["filing_id"] = pf.filing_id
            rec["source_url"] = f.source_url
            rec["filed_date"] = filed
            rec["row_order"] = i
            (all_tx if rec["record"] == "transaction" else all_pos).append(rec)

    assets_mod.annotate(all_tx)
    assets_mod.annotate(all_pos)
    registry = assets_mod.build_registry(all_tx + all_pos)
    merged = annotate_disclosure_delay(dedupe_transactions(all_tx))

    fd_years = [int(pf.meta["filing_year"]) for pf, _ in parsed
                if pf.doc_type == "fd" and pf.meta.get("filing_year")]
    period_end = date(max(fd_years), 12, 31) if fd_years else date(as_of.year - 1, 12, 31)
    holdings = reconstruct(all_pos, merged, as_of=as_of, period_end=period_end,
                           registry=registry, known_gaps=known_gaps or [])

    conn = db.connect(out_dir / "pelosi.db")
    with conn:
        for pf, url in parsed:
            db.upsert_filing(conn, pf, url, fetched_via="official")
        db.upsert_assets(conn, registry)
        db.insert_transactions(conn, merged)
        db.insert_positions(conn, all_pos)
        db.replace_holdings(conn, holdings)
    conn.close()

    (out_dir / "transactions.json").write_text(json.dumps(merged, indent=1))
    (out_dir / "positions.json").write_text(json.dumps(all_pos, indent=1))
    (out_dir / "assets.json").write_text(json.dumps(
        {k: v for k, v in sorted(registry.items())}, indent=1, default=list))
    (out_dir / "holdings.json").write_text(json.dumps(holdings, indent=1))

    for rec in merged:
        if rec["needs_review"]:
            review_detail.append({
                "filing_id": rec.get("filing_id"), "kind": "flagged_transaction",
                "asset": rec.get("asset_name"), "notes": rec.get("notes"),
                "raw": rec.get("description") or ""})
    for rec in all_pos:
        if rec["needs_review"]:
            review_detail.append({
                "filing_id": rec.get("filing_id"), "kind": "flagged_position",
                "asset": rec.get("asset_name"), "notes": rec.get("notes"),
                "raw": rec.get("asset_raw") or ""})
    (out_dir / "review.json").write_text(json.dumps(review_detail, indent=1))
    return {"filings": len(parsed), "raw_tx": len(all_tx), "merged": len(merged),
            "positions": len(all_pos), "assets": len(registry),
            "needs_review": len(review_detail), "known_gaps": known_gaps or [],
            "review_detail": review_detail,
            "holdings": holdings, "transactions": merged}


# ---------------------------------------------------------------- live mode --
def _iso(us_date: str | None) -> str | None:
    if not us_date:
        return None
    parts = us_date.split("/")
    if len(parts) == 3:
        m, d, y = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return us_date


def load_manifest(data_dir: Path) -> dict:
    mp = data_dir / "manifest.json"
    return json.loads(mp.read_text()) if mp.exists() else {}


def sync_live(data_dir: Path, years: list[int], last: str = "Pelosi",
              get=None) -> dict:
    """Diff the index, download + extract anything new, update the manifest.

    Manifest entry per DocID: index metadata, pdf_url, electronic flag, and
    text_path once extraction succeeded ("parseable" filings). Anything in the
    manifest without a text_path is a live known-gap.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "text").mkdir(exist_ok=True)
    manifest = load_manifest(data_dir)

    kwargs = {"get": get} if get else {}
    new_rows = ingest.sync(data_dir, years=years, last=last, **kwargs)
    for row in new_rows:
        manifest[row["DocID"]] = {
            "doc_id": row["DocID"], "filing_type": row.get("FilingType"),
            "filing_date": _iso(row.get("FilingDate")),
            "year": row.get("Year"), "pdf_url": row["pdf_url"],
            "electronic": row["electronic"], "text_path": None,
        }

    for doc_id, m in manifest.items():
        if m.get("text_path"):
            continue
        pdf = data_dir / "pdfs" / f"{doc_id}.pdf"
        if not m.get("electronic") or not pdf.exists():
            continue  # paper filing (OCR pending) or download failure: a gap
        try:
            text = ingest.extract_pdf_text(pdf)
        except Exception:
            continue  # stays a declared gap rather than a silent omission
        tp = data_dir / "text" / f"{doc_id}.txt"
        tp.write_text(text)
        m["text_path"] = str(tp)

    (data_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def run_live(data_dir: Path, out_dir: Path, years: list[int],
             last: str = "Pelosi", sync: bool = True, get=None) -> dict:
    if sync:
        manifest = sync_live(data_dir, years, last=last, get=get)
    else:
        manifest = load_manifest(data_dir)

    filings, gaps = [], []
    for doc_id, m in sorted(manifest.items()):
        if m.get("text_path") and Path(m["text_path"]).exists():
            filings.append(FilingInput(
                text=Path(m["text_path"]).read_text(),
                source_url=m["pdf_url"], doc_id=doc_id,
                filing_date=m.get("filing_date")))
        else:
            why = "paper filing pending OCR" if not m.get("electronic") \
                else "PDF not extracted"
            gaps.append(f"{m.get('filing_date') or '?'} #{doc_id} ({why})")

    if not filings:
        raise SystemExit("no parseable filings in the manifest; nothing to build")
    return build_dataset(filings, out_dir, known_gaps=gaps)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data", type=Path)
    ap.add_argument("--out", default="out", type=Path)
    ap.add_argument("--years", nargs="+", type=int,
                    default=[date.today().year - 1, date.today().year])
    ap.add_argument("--last", default="Pelosi")
    ap.add_argument("--no-sync", action="store_true",
                    help="rebuild from already-downloaded filings only")
    ap.add_argument("--fail-on-review", action="store_true",
                    help="exit 2 if any row needs human review (CI guard)")
    args = ap.parse_args(argv)

    info = run_live(args.data, args.out, args.years, last=args.last,
                    sync=not args.no_sync)
    print(f"filings={info['filings']} merged_tx={info['merged']} "
          f"positions={info['positions']} needs_review={info['needs_review']} "
          f"known_gaps={len(info['known_gaps'])}")
    for g in info["known_gaps"]:
        print(f"  gap: {g}")
    if args.fail_on_review and info["needs_review"]:
        print("rows need review - refusing to publish (accuracy over speed)",
              file=sys.stderr)
        print("--- review diagnostics (also in out/review.json) ---",
              file=sys.stderr)
        for d in info["review_detail"][:25]:
            raw = " ".join((d.get("raw") or "").split())[:240]
            print(f"[{d['kind']}] filing={d.get('filing_id')} "
                  f"section={d.get('section', '-')} asset={d.get('asset', '-')}\n"
                  f"  notes={d.get('notes')}\n  raw: {raw}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
