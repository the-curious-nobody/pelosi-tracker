"""Run the full pipeline over the five real Pelosi filings in fixtures/.

    python3 run_demo.py

Outputs (out/): transactions.json, positions.json, assets.json, holdings.json,
pelosi.db - plus a console summary. Fixture provenance:

    ptr_20026590  official PTR PDF, filed (signed) 2025-01-17
                  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20026590.pdf
    ptr_20033337  official PTR PDF, filed (signed) 2025-10-24
                  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20033337.pdf
    fd_10075701   official 2025 Annual Report PDF, filed 2026-05-15
                  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2025/10075701.pdf
    ptr_20033725  official PTR PDF, filed (signed) 2026-01-23
                  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033725.pdf
    ptr_20035143  official PTR PDF, filed (signed) 2026-08-21
                  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035143.pdf

Known gap: a PTR signed 2026-06-23 (May 29 INTC/UBER call purchases) is not
ingested - its official PDF was not retrievable here, and the pipeline does not
accept secondary reporting as a source.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pelosi_tracker import pipeline

ROOT = Path(__file__).parent
OUT = ROOT / "out"

FIXTURES = [
    ("ptr_20026590", "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20026590.pdf"),
    ("ptr_20033337", "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20033337.pdf"),
    ("fd_10075701", "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2025/10075701.pdf"),
    ("ptr_20033725", "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033725.pdf"),
    ("ptr_20035143", "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035143.pdf"),
]


def money(a: dict) -> str:
    if a is None:
        return "-"
    if a.get("is_none"):
        return "None"
    if a.get("undetermined"):
        return "Undetermined"
    if a.get("exact"):
        return f"{a.get('raw')} (exact)"
    lo, hi = a.get("min"), a.get("max")
    fmt = lambda v: f"${v:,.0f}"
    s = f"{fmt(lo)}-{fmt(hi)}" if lo and hi else (f"Over {fmt(lo)}" if lo else a.get("raw", "?"))
    return s + ("*" if a.get("max_inferred") else "")


def main() -> None:
    filings = [pipeline.FilingInput(
        text=(ROOT / "fixtures" / f"{name}.txt").read_text(), source_url=url)
        for name, url in FIXTURES]
    info = pipeline.build_dataset(
        filings, OUT,
        known_gaps=["2026-06-23 #20034836 (official PDF not retrieved in this "
                    "environment; live sync will fetch it)"])
    merged, holdings = info["transactions"], info["holdings"]
    flagged = info["needs_review"]

    print(f"Filings ingested: {info['filings']}   "
          f"raw tx rows: {info['raw_tx']}   merged transactions: {info['merged']}   "
          f"annual positions: {info['positions']}   assets: {info['assets']}")
    print(f"needs_review: {flagged}\n")

    print("=== Confirmed transactions (merged across PTR + annual Schedule B) ===")
    for t in merged:
        cor = "+".join(sorted({pr["schedule"] for pr in t.get("provenance", [])})) or t["source_schedule"]
        delay = t.get("disclosure_delay_days")
        facts = t["facts"]
        detail = facts["kind"]
        if facts["kind"] in ("option_purchase", "option_position") and facts["lots"]:
            l = facts["lots"][0]
            detail = f"{l['contracts']} {l['option_type']}s @${l['strike']:g} exp {l['expiration']}"
        elif facts["kind"] == "exercise":
            e = facts["exercise"]
            detail = f"exercised {e['contracts']} {e['option_type']}s @${e['strike']:g} -> {e['shares']:,} sh"
        elif facts.get("shares"):
            detail = f"{facts['shares']:,} shares" + (" (gift)" if facts["is_gift"] else "")
        elif facts.get("units"):
            detail = f"{facts['units']:,} units"
        print(f"  {t['transaction_date']}  {t['owner_code'] or '--':2} "
              f"{(t['ticker'] or t['asset_name'][:18]):18.18} "
              f"{t['transaction_code']:12} {money(t['amount']):>26}  "
              f"[{cor:6}] delay={delay if delay is not None else '-':>3}"
              f"{'^' if t.get('delay_basis') == 'annual_report_upper_bound' else ' '} {detail}")

    print("  (^ delay vs annual report only - an earlier PTR likely exists "
          "but is not yet ingested; upper bound)")
    print("\n=== Estimated holdings - stocks/securities "
          f"(baseline {holdings['baseline_period_end']}, as of {holdings['as_of']}) ===")
    for s in holdings["stocks"]:
        print(f"  {(s['ticker'] or s['asset_id'][:14]):14.14} "
              f"{money(s['period_end_value']):>26}  {s['status']:44.44} "
              f"[{s['confidence']}]")

    print("\n=== Estimated holdings - option lots ===")
    for o in holdings["options"]:
        print(f"  {o['asset_id']:6} {o['contracts']:>4} {o['option_type']}s "
              f"@${o['strike']:<7g} exp {o['expiration'] or '?':10}  "
              f"{o['status']:70.70} [{o['confidence']}]")

    print(f"\nOther (non-security) annual assets: {len(holdings['other_assets'])} "
          "(real property, LLC/LP interests, bank accounts, IP) - see holdings.json")
    if flagged:
        print(f"\n! {flagged} rows need review - see needs_review/notes in out/*.json")


if __name__ == "__main__":
    main()
