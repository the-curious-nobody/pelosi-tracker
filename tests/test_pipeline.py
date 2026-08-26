"""Live-path tests: prove the production pipeline (the code GitHub Actions
runs) works end-to-end without network access.

Run: python3 -m tests.test_pipeline
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pelosi_tracker import pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"

FIXTURE_META = {
    "20026590": ("ptr_20026590", "P", "2025-01-17", "2025"),
    "20033337": ("ptr_20033337", "P", "2025-10-24", "2025"),
    "10075701": ("fd_10075701", "O", "2026-05-15", "2025"),
    "20033725": ("ptr_20033725", "P", "2026-01-23", "2026"),
    "20035143": ("ptr_20035143", "P", "2026-08-21", "2026"),
}


def _make_data_dir(td: Path) -> Path:
    """Simulate the state ingest.sync + extraction leave behind, plus one
    un-extracted paper filing (the live known-gap case)."""
    data = td / "data"
    (data / "text").mkdir(parents=True)
    manifest = {}
    for doc_id, (name, ftype, fdate, year) in FIXTURE_META.items():
        tp = data / "text" / f"{doc_id}.txt"
        tp.write_text((FIX / f"{name}.txt").read_text())
        sub = "ptr-pdfs" if ftype == "P" else "financial-pdfs"
        manifest[doc_id] = {
            "doc_id": doc_id, "filing_type": ftype, "filing_date": fdate,
            "year": year, "electronic": True, "text_path": str(tp),
            "pdf_url": f"https://disclosures-clerk.house.gov/public_disc/{sub}/{year}/{doc_id}.pdf",
        }
    manifest["8219999"] = {  # paper filing: indexed, never extracted
        "doc_id": "8219999", "filing_type": "P", "filing_date": "2026-06-23",
        "year": "2026", "electronic": False, "text_path": None,
        "pdf_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/8219999.pdf",
    }
    (data / "manifest.json").write_text(json.dumps(manifest))
    return data


def test_run_live_matches_fixture_demo():
    """The manifest-driven production path must produce the same dataset the
    fixture demo does - one core, two doors."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data = _make_data_dir(td)
        info = pipeline.run_live(data, td / "out",
                                 years=[2025, 2026], sync=False)
        assert info["filings"] == 5
        assert info["merged"] == 37 and info["raw_tx"] == 54
        assert info["positions"] == 68
        assert info["needs_review"] == 0
        # the gap is computed from the manifest, not hand-declared
        assert info["known_gaps"] == ["2026-06-23 #8219999 (paper filing pending OCR)"]
        h = json.loads((td / "out" / "holdings.json").read_text())
        assert h["coverage"]["known_gaps"] == info["known_gaps"]
        assert h["coverage"]["filed_through"] == "2026-08-21"
        # index DocID is authoritative for every record
        tx = json.loads((td / "out" / "transactions.json").read_text())
        assert {t["filing_id"] for t in tx} <= set(FIXTURE_META)


def test_sync_live_manifest_and_gap_handling():
    """sync_live against a synthetic index: paper filings are never extracted,
    garbage PDFs fail extraction, and both end up as declared gaps."""
    xml = b"""<FinancialDisclosure>
      <Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><Suffix/>
        <FilingType>P</FilingType><StateDst>CA11</StateDst><Year>2026</Year>
        <FilingDate>8/24/2026</FilingDate><DocID>20099999</DocID></Member>
      <Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><Suffix/>
        <FilingType>P</FilingType><StateDst>CA11</StateDst><Year>2026</Year>
        <FilingDate>6/23/2026</FilingDate><DocID>8219999</DocID></Member>
    </FinancialDisclosure>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2026FD.xml", xml)

    def fake_get(url: str) -> bytes:
        if url.endswith("FD.zip"):
            return buf.getvalue()
        return b"%PDF-1.4 not really a pdf"

    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        manifest = pipeline.sync_live(data, years=[2026], get=fake_get)
        assert set(manifest) == {"20099999", "8219999"}
        e, paper = manifest["20099999"], manifest["8219999"]
        assert e["electronic"] and not paper["electronic"]
        assert e["filing_date"] == "2026-08-24"
        # garbage bytes can't extract; both remain gaps, loudly
        assert e["text_path"] is None and paper["text_path"] is None
        assert (data / "pdfs" / "20099999.pdf").exists()
        # idempotent: a second sync sees nothing new, manifest persists
        manifest2 = pipeline.sync_live(data, years=[2026], get=fake_get)
        assert set(manifest2) == set(manifest)


if __name__ == "__main__":
    test_run_live_matches_fixture_demo()
    print("ok  test_run_live_matches_fixture_demo")
    test_sync_live_manifest_and_gap_handling()
    print("ok  test_sync_live_manifest_and_gap_handling")
    print("\n2 tests passed")
