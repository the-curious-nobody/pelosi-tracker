"""Regression tests against the three real Pelosi filings in fixtures/.

Run:  python3 -m tests.test_parsers      (plain asserts, no pytest needed)
      pytest tests/                      (also works)

Ground truth was verified by eye against the official PDFs:
  PTR 20026590 (signed 2025-01-17), PTR 20033337 (signed 2025-10-24),
  Annual FD 10075701 for CY2025 (filed 2026-05-15).
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path

from pelosi_tracker import assets as assets_mod
from pelosi_tracker import ingest
from pelosi_tracker.amounts import AmountRange
from pelosi_tracker.filings import parse_filing_text
from pelosi_tracker.holdings import (annotate_disclosure_delay,
                                     dedupe_transactions, reconstruct)
from pelosi_tracker.textnorm import decode_glyphs

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"


def _load(name: str):
    return parse_filing_text((FIX / f"{name}.txt").read_text(), source=name)


def _by(txs, **kw):
    out = [t for t in txs if all(t.get(k) == v for k, v in kw.items())]
    assert out, f"no transaction matching {kw}"
    return out


# --------------------------------------------------------------------------

def test_glyph_cipher():
    encoded = "".join(chr(0x0283 + (ord(c) - ord("a"))) for c in "description")
    assert decode_glyphs("D" + encoded[1:] + ":") == "Description:"
    assert decode_glyphs("Pʇʔʋʑʆʋʅ Tʔʃʐʕʃʅʖʋʑʐ Rʇʒʑʔʖ") == "Periodic Transaction Report"


def test_amount_buckets():
    a = AmountRange.parse("$1,000,001 -", table="transaction")
    assert (a.min, a.max, a.max_inferred) == (1_000_001, 5_000_000, True)
    assert AmountRange.parse("Over $50,000,000").max is None
    assert AmountRange.parse("None").is_none
    assert AmountRange.parse("$5,001 - $15,000", table="income").in_known_bucket
    # income tier that is NOT a transaction bucket
    assert AmountRange.parse("$100,001 - $1,000,000", table="income").in_known_bucket
    assert not AmountRange.parse("$100,001 - $1,000,000", table="transaction").in_known_bucket


def test_ptr_20033337_gift():
    pf = _load("ptr_20033337")
    assert pf.doc_type == "ptr" and pf.filing_id == "20033337"
    assert pf.signed_date == "2025-10-24"
    (t,) = pf.transactions()
    assert t["ticker"] == "AAPL" and t["asset_type_code"] == "ST"
    assert t["owner_code"] == "SP"
    assert t["transaction_code"] == "S (partial)"
    assert t["transaction_date"] == "2025-10-22"
    assert (t["amount"]["min"], t["amount"]["max"]) == (100_001, 250_000)
    assert t["facts"]["kind"] == "gift" and t["facts"]["is_gift"]
    assert t["facts"]["shares"] == 382
    assert t["filing_status"] == "New"


def test_ptr_20026590_nine_rows():
    pf = _load("ptr_20026590")
    txs = pf.transactions()
    assert len(txs) == 9
    assert all(t["owner_code"] == "SP" for t in txs)
    assert not any(t["needs_review"] for t in txs)

    # page-break mid-row: the NVDA [OP] description is separated from its row
    # by the Filing ID footer and a repeated header block - must reattach.
    (nvda_op,) = _by(txs, ticker="NVDA", asset_type_code="OP")
    (lot,) = nvda_op["facts"]["lots"]
    assert (lot["contracts"], lot["strike"], lot["expiration"]) == (50, 80.0, "2026-01-16")

    # exercise with wrapped description and single purchase date
    (nvda_ex,) = [t for t in txs if t["facts"]["kind"] == "exercise" and t["ticker"] == "NVDA"]
    e = nvda_ex["facts"]["exercise"]
    assert (e["contracts"], e["strike"], e["shares"]) == (500, 12.0, 50_000)
    assert e["purchase_dates"] == ["2023-11-22"]
    assert e["expiration"] == "2024-12-20"

    # exercise with TWO purchase dates ("2/12/24 & 2/21/24")
    (panw,) = _by(txs, ticker="PANW")
    e = panw["facts"]["exercise"]
    assert e["purchase_dates"] == ["2024-02-12", "2024-02-21"]
    assert (e["contracts"], e["shares"]) == (140, 14_000)

    (aapl,) = _by(txs, ticker="AAPL")
    assert aapl["facts"]["shares"] == 31_600
    assert (aapl["amount"]["min"], aapl["amount"]["max"]) == (5_000_001, 25_000_000)


def test_fd_10075701_positions_and_schedule_b():
    pf = _load("fd_10075701")
    assert pf.doc_type == "fd" and pf.filing_id == "10075701"
    assert pf.meta["filing_year"] == "2025" and pf.signed_date == "2026-05-15"
    pos, txs = pf.assets(), pf.transactions()
    assert len(pos) == 68 and len(txs) == 19

    # multi-lot option position (two expirations in one description)
    (nvda_op,) = [p for p in pos if p["ticker"] == "NVDA" and p["asset_type_code"] == "OP"]
    lots = nvda_op["facts"]["lots"]
    assert [(l["contracts"], l["strike"], l["expiration"]) for l in lots] == \
        [(50, 80.0, "2026-01-16"), (20, 100.0, "2027-01-15")]

    # value=None rows (position gone at year end, income proves it existed)
    for tk in ("DIS", "PYPL"):
        (p,) = [p for p in pos if p["ticker"] == tk and p["asset_type_code"] == "ST"]
        assert p["value"]["is_none"] and p["income"] is not None

    # page-break value re-stitch ("$100,001 - <headers> None <newpage> $250,000")
    (fls,) = [p for p in pos if p["asset_name"].startswith("Financial Leasing")]
    assert (fls["value"]["min"], fls["value"]["max"]) == (100_001, 250_000)
    assert any("re-stitched" in n for n in fls["notes"])

    # owner column may be blank (filer)
    (ccu,) = [p for p in pos if p["asset_name"].startswith("Congressional Credit Union")]
    assert ccu["owner_code"] is None
    assert (ccu["value"]["min"], ccu["value"]["max"]) == (250_001, 500_000)

    # Schedule B: dangling amount repaired from the statutory bucket, flagged
    (avgo,) = _by(txs, ticker="AVGO")
    assert (avgo["amount"]["min"], avgo["amount"]["max"]) == (1_000_001, 5_000_000)
    assert avgo["amount"]["max_inferred"]
    assert avgo["facts"]["kind"] == "exercise"
    assert avgo["facts"]["exercise"]["shares"] == 20_000

    # unit sale with Location + Comments labels
    (mat,) = [t for t in txs if t["asset_name"].startswith("Matthews")]
    assert mat["facts"]["units"] == 2_822
    assert mat["location"] == "US" and "28,948" in mat["comments"]

    # gifts vs open-market sales distinguished
    gifts = [t for t in txs if t["facts"]["is_gift"]]
    assert {(t["ticker"], t["facts"]["shares"]) for t in gifts} == \
        {("AAPL", 382), ("AAPL", 28_200), ("GOOGL", 7_704)}

    # liabilities captured raw (Schedule D) for provenance
    assert any("Charles Schwab" in l for l in pf.other_sections["D"])


def _pipeline():
    all_tx, all_pos, parsed = [], [], []
    for name in ("ptr_20026590", "ptr_20033337", "fd_10075701"):
        pf = _load(name)
        parsed.append(pf)
        filed = pf.signed_date or pf.meta.get("filing_date")
        for i, r in enumerate(pf.rows):
            if not r.parsed:
                continue
            rec = dict(r.parsed)
            rec.update(filing_id=pf.filing_id, filed_date=filed, row_order=i)
            (all_tx if rec["record"] == "transaction" else all_pos).append(rec)
    assets_mod.annotate(all_tx)
    assets_mod.annotate(all_pos)
    registry = assets_mod.build_registry(all_tx + all_pos)
    merged = dedupe_transactions(all_tx)
    return all_pos, merged, registry


def test_cross_source_dedupe():
    _, merged, _ = _pipeline()
    assert len(merged) == 23              # 29 raw rows, 6 duplicates merged
    dupes = [t for t in merged if t["corroborated"]]
    assert len(dupes) == 6
    (gift,) = [t for t in dupes if t["facts"]["is_gift"]]
    assert {p["schedule"] for p in gift["provenance"]} == {"PTR", "FD-B"}
    # PTR wins as base: notification date survives the merge
    assert gift["notification_date"] == "2025-10-22"


def test_holdings_reconstruction():
    pos, merged, registry = _pipeline()
    h = reconstruct(pos, merged, as_of=date(2026, 8, 25),
                    period_end=date(2025, 12, 31), registry=registry)

    stocks = {s["asset_id"]: s for s in h["stocks"]}
    assert stocks["DIS"]["status"].startswith("Closed")
    assert stocks["DIS"]["confidence"] == "high"
    assert stocks["AAPL"]["status"] == "Likely Held"
    # staleness must degrade confidence, never silently stay high
    assert stocks["AAPL"]["confidence"] == "medium"
    assert any("in filings ingested through" in n for n in stocks["AAPL"]["notes"])
    moves = {m["date"]: m["shares"] for m in stocks["AAPL"]["disclosed_movements"]}
    assert moves["2025-12-24"] == -45_000 and moves["2024-12-31"] == -31_600

    lots = {(o["asset_id"], o["strike"], o["expiration"]): o for o in h["options"]}
    avgo = lots[("AVGO", 80.0, "2025-06-20")]
    assert avgo["status"].startswith("Exercised") and avgo["confidence"] == "high"
    jan26 = [o for o in h["options"] if o["expiration"] == "2026-01-16"]
    assert len(jan26) == 5
    assert all(o["confidence"] == "low" and "not present" in o["status"] for o in jan26)
    jan27 = [o for o in h["options"] if o["expiration"] == "2027-01-15"]
    assert len(jan27) == 4
    assert all(o["status"] == "Likely Held" for o in jan27)
    # no share balances anywhere - only disclosed movements
    assert "share balances are never computed" in h["method"]


def test_ingest_index_roundtrip(tmp_path=None):
    xml = b"""<FinancialDisclosure>
      <Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><Suffix/>
        <FilingType>P</FilingType><StateDst>CA11</StateDst><Year>2026</Year>
        <FilingDate>8/24/2026</FilingDate><DocID>20035143</DocID></Member>
      <Member><Prefix>Hon.</Prefix><Last>Someone</Last><First>Else</First><Suffix/>
        <FilingType>O</FilingType><StateDst>TX01</StateDst><Year>2026</Year>
        <FilingDate>5/15/2026</FilingDate><DocID>10088888</DocID></Member>
    </FinancialDisclosure>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2026FD.xml", xml)
    rows = ingest.fetch_index(2026, get=lambda url: buf.getvalue())
    assert len(rows) == 2
    mine = ingest.filter_member(rows, last="Pelosi")
    assert len(mine) == 1 and mine[0]["DocID"] == "20035143"
    assert ingest.pdf_url(mine[0]).endswith("/public_disc/ptr-pdfs/2026/20035143.pdf")
    assert ingest.pdf_url(rows[1]).endswith("/public_disc/financial-pdfs/2026/10088888.pdf")
    assert ingest.is_electronic("20035143") and not ingest.is_electronic("8220000")


def _pipeline_all():
    """Full ingest including the two 2026 PTRs."""
    all_tx, all_pos = [], []
    for name in ("ptr_20026590", "ptr_20033337", "fd_10075701",
                 "ptr_20033725", "ptr_20035143"):
        pf = _load(name)
        filed = pf.signed_date or pf.meta.get("filing_date")
        for i, r in enumerate(pf.rows):
            if not r.parsed:
                continue
            rec = dict(r.parsed)
            rec.update(filing_id=pf.filing_id, filed_date=filed, row_order=i)
            (all_tx if rec["record"] == "transaction" else all_pos).append(rec)
    assets_mod.annotate(all_tx)
    assets_mod.annotate(all_pos)
    registry = assets_mod.build_registry(all_tx + all_pos)
    return all_pos, annotate_disclosure_delay(dedupe_transactions(all_tx)), registry


def test_ptr_20033725_exercises_and_exact_amount():
    """Jan 2026 PTR: exercises of the 1/16/26 lots, a spinoff row carrying an
    exact dollar figure, and an asset name split across a page break."""
    pf = _load("ptr_20033725")
    rows = [r.parsed for r in pf.rows if r.parsed]
    assert len(rows) == 18, len(rows)
    assert not any(r["needs_review"] for r in rows)

    # an exact filed figure must not be mistaken for a truncated range
    vsnt = _by(rows, ticker="VSNT")[0]
    assert vsnt["transaction_code"] == "E"
    assert vsnt["amount"]["exact"] is True
    assert vsnt["amount"]["min"] == vsnt["amount"]["max"] == 15
    assert vsnt["amount"]["max_inferred"] is False

    # page break stranded "Stock (TEM) [ST]" inside the amount cell
    tem = _by(rows, ticker="TEM")[0]
    assert tem["asset_type_code"] == "ST"
    assert tem["amount"]["min"] == 50_001 and tem["amount"]["max"] == 100_000
    assert tem["facts"]["exercise"]["strike"] == 20.0

    exercises = [r for r in rows if r["facts"]["kind"] == "exercise"]
    assert {r["ticker"] for r in exercises} == {"GOOGL", "AMZN", "NVDA", "TEM", "VST"}
    assert all(e["facts"]["exercise"]["shares"] == 5_000 for e in exercises)


def test_stock_and_option_same_day_not_merged():
    """BE stock and BE calls bought the same day in the same amount bucket are
    two distinct trades; merging them would delete a disclosed transaction."""
    _, merged, _ = _pipeline_all()
    be = _by(merged, asset_id="BE", transaction_date="2026-07-24")
    assert len(be) == 2, be
    assert {t["asset_type_code"] for t in be} == {"ST", "OP"}


def test_2026_filings_resolve_lots_and_delays():
    pos, merged, registry = _pipeline_all()
    h = reconstruct(pos, merged, as_of=date(2026, 8, 26),
                    period_end=date(2025, 12, 31), registry=registry)
    lots = {(o["asset_id"], o["strike"], o["expiration"]): o for o in h["options"]}

    # every 1/16/26 lot is now resolved by the January PTR, not left unknown
    expired = {k[0] for k in lots if k[2] == "2026-01-16"}
    assert expired == {"GOOGL", "AMZN", "NVDA", "TEM", "VST"}
    for aid in expired:
        o = next(v for k, v in lots.items() if k[0] == aid and k[2] == "2026-01-16")
        assert o["status"].startswith("Exercised") and o["confidence"] == "high"

    # repeat purchases of an identical lot accumulate...
    be = lots[("BE", 100.0, "2027-06-17")]
    assert be["contracts"] == 200 and be["acquired"] == "2026-07-24"
    assert any("2 disclosed purchases" in n for n in be["notes"])
    # ...while an annual snapshot is never double-counted by its own purchase
    assert lots[("AAPL", 100.0, "2027-01-15")]["contracts"] == 20

    # the real PTR replaces the annual-report upper bound for December trades
    dec = _by(merged, asset_id="DIS", transaction_date="2025-12-30")[0]
    assert dec["corroborated"] and dec["delay_basis"] == "ptr"
    assert dec["disclosure_delay_days"] == 24
    assert sum(1 for t in merged if t["corroborated"]) == 17

    # spinoff receipt parses as shares received, with an exact amount
    vsnt = _by(merged, asset_id="VSNT")[0]
    assert vsnt["facts"]["kind"] == "shares_received"
    assert vsnt["facts"]["shares"] == 776
    assert vsnt["amount"]["exact"] is True

    # a share purchase is never labelled a sale
    ab_tx = _by(merged, asset_id="AB")[0]
    assert ab_tx["facts"]["kind"] == "share_purchase"


def test_asset_routing_and_coverage():
    """Non-securities stay out of the securities table; dual-classified assets
    cross-reference instead of contradicting; coverage is stated honestly."""
    pos, merged, registry = _pipeline_all()
    h = reconstruct(pos, merged, as_of=date(2026, 8, 26),
                    period_end=date(2025, 12, 31), registry=registry,
                    known_gaps=["2026-06-23"])

    stock_ids = {s["asset_id"] for s in h["stocks"]}
    assert "reof-xxv-llc" not in stock_ids          # LLC follow-on is not a stock
    assert "AB" not in stock_ids                    # filer classifies AB as [OL]/[AB]

    other = {o["asset_id"]: o for o in h["other_assets"]}
    assert any("2026-07-27" in n for n in other["reof-xxv-llc"]["notes"])
    assert any("2026-01-16" in n for n in other["AB"]["notes"])
    # each asset tells one story: annotated, never duplicated
    assert sum(1 for o in h["other_assets"] if o["asset_id"] == "AB") == 1

    vsnt = next(s for s in h["stocks"] if s["asset_id"] == "VSNT")
    assert "corporate action" in vsnt["status"]
    assert vsnt["disclosed_movements"][0]["shares"] == 776

    assert h["coverage"] == {"filed_through": "2026-08-21",
                             "known_gaps": ["2026-06-23"]}
    aapl = next(s for s in h["stocks"] if s["asset_id"] == "AAPL")
    assert any("known filing(s) not ingested" in n for n in aapl["notes"])


def test_pdftotext_layout_geometry():
    """Regression suite built from the VERBATIM row text that the first live
    run's diagnostics captured (pdftotext -layout on the GitHub runner).
    Every case here failed or flagged in production before this fix."""
    from pelosi_tracker.filings import Row, parse_fda_row, parse_ptr_row
    from pelosi_tracker.textnorm import is_furniture

    # -layout emits header fragments my split-form furniture missed
    assert is_furniture("Type Gains >")
    assert is_furniture("Type Date Gains >")

    # displaced type code: both wrapped range halves flung to the row's end
    r = Row(section="A", text=(
        "45 Belden Place - Four Story Commercial Building SP $5,000,001 - "
        "Rent $100,001 - [RP] $25,000,000 $1,000,000"), order=0)
    parse_fda_row(r)
    assert r.parsed and not r.needs_review
    assert r.parsed["asset_type_code"] == "RP"
    assert r.parsed["value"]["min"] == 5_000_001
    assert r.parsed["value"]["max"] == 25_000_000
    assert r.parsed["value"]["max_inferred"] is False   # confirmed, not guessed
    assert r.parsed["income"]["min"] == 100_001
    assert r.parsed["income"]["max"] == 1_000_000
    assert r.parsed["income_types"] == "Rent"

    # displaced code with value=range, income=None
    r = Row(section="A", text=(
        "Alphabet Inc. - Class A Common Stock (GOOGL) SP $1,000,001 - "
        "None [OP] $5,000,000"), order=0)
    parse_fda_row(r)
    assert r.parsed and not r.needs_review
    assert r.parsed["ticker"] == "GOOGL" and r.parsed["asset_type_code"] == "OP"
    assert r.parsed["value"]["max"] == 5_000_000
    assert r.parsed["value"]["max_inferred"] is False
    assert r.parsed["income"]["is_none"] is True

    r = Row(section="A", text=(
        "Tempus AI, Inc. - Class A Common Stock (TEM) SP $100,001 - "
        "None [OP] $250,000"), order=0)
    parse_fda_row(r)
    assert r.parsed and not r.needs_review and r.parsed["value"]["max"] == 250_000

    # wrapped income-type words trailing the income amount
    r = Row(section="A", text=(
        "Borel Real Estate Company [OL] SP $15,001 - $50,000 "
        "Partnership $1,001 - $2,500 Income, Rent"), order=0)
    parse_fda_row(r)
    assert r.parsed and not r.needs_review
    assert r.parsed["income_types"] == "Partnership Income, Rent"
    assert r.parsed["income"]["max"] == 2_500

    # double wrap: value max AND income max both stranded, plus a type word
    r = Row(section="A", text=(
        "NVIDIA Corporation - Common Stock (NVDA) [ST] SP $5,000,001 - "
        "Capital Gains, $1,000,001 - $25,000,000 Dividends $5,000,000"), order=0)
    parse_fda_row(r)
    assert r.parsed and not r.needs_review
    assert r.parsed["value"]["max"] == 25_000_000
    assert r.parsed["income"]["min"] == 1_000_001
    assert r.parsed["income"]["max"] == 5_000_000
    assert "Dividends" in r.parsed["income_types"]

    # PTR: lone displaced "[OT]" joined onto a complete row's amount tail
    r = Row(section="T", text=(
        "SP Matthews International Mutual Fund S 06/20/2025 06/20/2025 "
        "$15,001 - $50,000 [OT]"), order=0)
    parse_ptr_row(r)
    assert r.parsed and not r.needs_review
    assert r.parsed["asset_type_code"] == "OT"
    assert r.parsed["amount"]["min"] == 15_001 and r.parsed["amount"]["max"] == 50_000

    # genuine anomalies must STILL flag: unexplained money in the tail
    r = Row(section="A", text=(
        "Palo Alto Networks, Inc. - Common Stock (PANW) SP $1,000,001 - "
        "None [ST] $5,000,000 Loss $1,000,000"), order=0)
    parse_fda_row(r)
    assert r.parsed and r.needs_review
    assert any("unattached" in n for n in r.parsed["notes"])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
