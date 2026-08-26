"""Ingestion worker for the House Clerk disclosure index.

Update flow (per product spec section 4):

    {YEAR}FD.zip index  ->  diff DocIDs  ->  download official PDF
    ->  extract text    ->  parse (filings.py)  ->  store (db.py)

The Clerk publishes a yearly ZIP at
    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip
containing {YEAR}FD.xml (and a TSV twin) with one row per filing:
Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate, DocID.
The ZIP is republished daily as new filings arrive, so polling it and diffing
DocIDs against local state is the whole change-detection story - no scraping
of the search UI, no third-party trackers.

Document URLs are deterministic from the index row:
    PTRs        /public_disc/ptr-pdfs/{Year}/{DocID}.pdf
    everything  /public_disc/financial-pdfs/{Year}/{DocID}.pdf

NOTE - runtime environments: this module uses only the stdlib (urllib).
In Claude's sandbox the Clerk's domain is not on the network allowlist, so
sync() cannot run there; it is exercised by unit tests against a synthetic
index and is ready to run on any machine with normal egress (or add
disclosures-clerk.house.gov to the sandbox allowlist).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

BASE = "https://disclosures-clerk.house.gov"
INDEX_URL = BASE + "/public_disc/financial-pdfs/{year}FD.zip"
USER_AGENT = "pelosi-tracker/0.1 (public-records research; contact site operator)"

# Observed filing-type codes in the index (non-authoritative, verify on live
# data): P=Periodic Transaction Report, O=Original annual, A=Amendment,
# C=Candidate report, T=Termination, X=Extension, D=Blind trust/other.
PTR_TYPES = {"P"}

INDEX_FIELDS = ["Prefix", "Last", "First", "Suffix",
                "FilingType", "StateDst", "Year", "FilingDate", "DocID"]


def pdf_url(row: dict) -> str:
    sub = "ptr-pdfs" if row["FilingType"] in PTR_TYPES else "financial-pdfs"
    return f"{BASE}/public_disc/{sub}/{row['Year']}/{row['DocID']}.pdf"


def is_electronic(doc_id: str) -> bool:
    """Heuristic: e-filed DocIDs start with 1 (FD) or 2 (PTR); paper scans use
    other prefixes and need OCR. Verify against live data before trusting."""
    return doc_id.startswith(("1", "2"))


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def parse_index_xml(data: bytes) -> list[dict]:
    rows = []
    root = ET.fromstring(data)
    for member in root.iter():
        children = {c.tag: (c.text or "").strip() for c in member}
        if "DocID" in children and children.get("DocID"):
            rows.append({f: children.get(f, "") for f in INDEX_FIELDS})
    return rows


def parse_index_tsv(text: str) -> list[dict]:
    lines = [l for l in text.splitlines() if l.strip()]
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        vals = line.split("\t")
        row = dict(zip(header, vals))
        if row.get("DocID"):
            rows.append({f: row.get(f, "").strip() for f in INDEX_FIELDS})
    return rows


def fetch_index(year: int, get=_http_get) -> list[dict]:
    blob = get(INDEX_URL.format(year=year))
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if xml_names:
            return parse_index_xml(zf.read(xml_names[0]))
        txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if txt_names:
            return parse_index_tsv(zf.read(txt_names[0]).decode("utf-8", "replace"))
    raise ValueError("no index file inside ZIP")


def filter_member(rows: list[dict], last: str = "Pelosi",
                  first: str | None = None) -> list[dict]:
    out = [r for r in rows if r["Last"].strip().lower() == last.lower()]
    if first:
        out = [r for r in out if r["First"].strip().lower().startswith(first.lower())]
    return out


def extract_pdf_text(pdf_path: Path) -> str:
    """pdftotext -layout preferred (matches the fixtures' shape); pdfplumber
    fallback. Both feed the same normalization layer (textnorm)."""
    if shutil.which("pdftotext"):
        res = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"],
                             capture_output=True, text=True, timeout=120)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout
    try:
        import pdfplumber  # optional dependency
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError as exc:
        raise RuntimeError(
            "no PDF text extractor available: install poppler-utils "
            "(pdftotext) or `pip install pdfplumber`") from exc


def sync(data_dir: str | Path, years: list[int], last: str = "Pelosi",
         get=_http_get, download: bool = True) -> list[dict]:
    """Diff the index against local state; fetch anything new.

    Returns the list of new index rows (with local paths when downloaded).
    State lives in {data_dir}/state.json; PDFs in {data_dir}/pdfs/.
    """
    data_dir = Path(data_dir)
    (data_dir / "pdfs").mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": []}
    seen = set(state["seen"])

    new_rows = []
    for year in years:
        for row in filter_member(fetch_index(year, get=get), last=last):
            if row["DocID"] in seen:
                continue
            row = dict(row)
            row["pdf_url"] = pdf_url(row)
            row["electronic"] = is_electronic(row["DocID"])
            row["needs_ocr"] = not row["electronic"]
            if download:
                dest = data_dir / "pdfs" / f"{row['DocID']}.pdf"
                dest.write_bytes(get(row["pdf_url"]))
                row["local_path"] = str(dest)
            new_rows.append(row)
            seen.add(row["DocID"])

    state["seen"] = sorted(seen)
    state_path.write_text(json.dumps(state, indent=1))
    return new_rows


_AMENDMENT_TYPES = {"A"}


def classify_index_row(row: dict) -> str:
    t = row.get("FilingType", "")
    if t in PTR_TYPES:
        return "ptr"
    if t in _AMENDMENT_TYPES:
        return "fd_amendment"
    return "fd"
