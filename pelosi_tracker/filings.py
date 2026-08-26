"""Parse House Clerk e-filed disclosure documents (PTRs and Annual FD reports).

Input is plain text extracted from the official PDFs (pdftotext -layout,
pdfplumber, or equivalent). The extraction wraps table cells across lines and
interleaves page furniture (repeated column headers, "Filing ID #...") into
the middle of logical rows, so parsing is a line-oriented state machine:

  - furniture lines are skipped even mid-row;
  - a row buffer accumulates lines until the row grammar is satisfied;
  - sub-row labels (Filing Status / Description / Location / Comments) attach
    to the most recently finalized row, and Description/Comments may wrap;
  - truncated amount ranges are repaired from the statutory bucket table and
    flagged (never silently asserted).

Grammars:
  PTR row : [OWNER] ASSET [CODE] TYPE DATE NOTIF_DATE AMOUNT
  FD-B row: ASSET [CODE] [OWNER] DATE TYPE AMOUNT
  FD-A row: ASSET [CODE] [OWNER] VALUE [INCOME_TYPES INCOME]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .amounts import AmountRange, RANGE_RE, DANGLING_RE, INCOME_MIN_TO_MAX
from .descriptions import parse_description, parse_us_date
from . import textnorm as tn

DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")
CODE_RE = re.compile(r"\[([A-Z0-9]{2})\]")
OWNER_LEAD = re.compile(r"^(SP|JT|DC)\b")
TICKER_PARENS = re.compile(r"\(([A-Z][A-Z0-9.&]{0,6})\)")
CITY_RE = re.compile(r"^[A-Za-z0-9 .'/\-]+,\s*[A-Z]{2}$")
SECTION_RE = re.compile(r"^S(?:chedule)?\s+([A-I])\b\s*:?", re.I)

PTR_ANCHOR = re.compile(
    r"\b(?P<t>[PSE])\s*(?:\(\s*(?P<sub>partial)\s*\))?\s+"
    r"(?P<d1>\d{1,2}/\d{1,2}/\d{4})\s+(?P<d2>\d{1,2}/\d{1,2}/\d{4})\s+(?P<amt>\S.*)$"
)
FDB_ANCHOR = re.compile(
    r"(?:\b(?P<owner>SP|JT|DC)\s+)?"
    r"(?P<d1>\d{1,2}/\d{1,2}/\d{4})\s+(?P<t>[PSE])\b\s*"
    r"(?:\(\s*(?P<sub>partial)\s*\))?\s*(?P<amt>.*)$"
)
AMT_COMPLETE = re.compile(r"(?:\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+)\s*$")
VALUE_TOKEN = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+|\$[\d,]+\s*-|None\b|Undetermined\b")
OWNER_OR_VALUE = re.compile(r"\b(?:SP|JT|DC)\b|" + VALUE_TOKEN.pattern)
LONE_CODE = re.compile(r"^\[[A-Z0-9]{2}\]$")
TRAILING_CODE = re.compile(r"\s*(\[[A-Z0-9]{2}\])\s*$")
INCOME_TYPE_VOCAB = frozenset(
    "income rent loss dividends interest capital gains partnership "
    "royalties sales grape".split())


def _is_income_type_words(txt: str) -> bool:
    words = [w for w in re.split(r"[,\s]+", txt.strip()) if w]
    return bool(words) and all(w.lower() in INCOME_TYPE_VOCAB for w in words)


A_TAIL = re.compile(
    r"^(?:\$|None\b|Undetermined\b|Over \$|Dividends|Interest\b|Rent\b|Capital\b|"
    r"Partnership|Royalties|Grape\b|Income\b|Loss\b|Salary|Management|N/A)"
)
# page-break artifact: value range split around an interleaved "None" income cell
STITCH_RE = re.compile(r"(\$[\d,]+)\s*-\s+None\s+(\$[\d,]+)\b")
# page-break artifact: asset-name tail stranded inside a split amount range
ASSET_IN_AMOUNT = re.compile(
    r"^(?P<lo>\$[\d,]+\s*-)\s+(?P<frag>\S.*?\[[A-Z0-9]{2}\])\s+(?P<hi>\$[\d,]+)\s*$"
)


@dataclass
class Row:
    section: str                       # "T" (PTR), "A", "B"
    text: str
    order: int
    filing_status: str | None = None
    description: str | None = None
    location: str | None = None
    comments: str | None = None
    parsed: dict | None = None
    needs_review: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ParsedFiling:
    doc_type: str                      # "ptr" | "fd"
    source: str | None
    meta: dict = field(default_factory=dict)
    filing_ids: list[str] = field(default_factory=list)
    signed_by: str | None = None
    signed_date: str | None = None
    rows: list[Row] = field(default_factory=list)
    other_sections: dict = field(default_factory=dict)   # raw lines for FD C..I

    @property
    def filing_id(self) -> str | None:
        return self.filing_ids[0] if self.filing_ids else None

    def transactions(self) -> list[dict]:
        return [r.parsed for r in self.rows
                if r.parsed and r.parsed.get("record") == "transaction"]

    def assets(self) -> list[dict]:
        return [r.parsed for r in self.rows
                if r.parsed and r.parsed.get("record") == "position"]


# --------------------------------------------------------------------------
# row-text parsers
# --------------------------------------------------------------------------

def _split_owner_asset(pre: str) -> tuple[str | None, str]:
    m = OWNER_LEAD.match(pre)
    if m:
        return m.group(1), pre[m.end():].strip()
    return None, pre.strip()


def _asset_fields(asset_part: str) -> dict:
    code_m = CODE_RE.search(asset_part)
    code = code_m.group(1) if code_m else None
    name = CODE_RE.sub(" ", asset_part)
    name = re.sub(r"\s+", " ", name).strip().rstrip(",")
    tickers = TICKER_PARENS.findall(name)
    return {"asset_raw": asset_part.strip(), "asset_name": name,
            "asset_type_code": code, "ticker": tickers[-1] if tickers else None}


def _amount(raw: str, row: Row, table: str = "asset") -> AmountRange:
    a = AmountRange.parse(raw, table=table)
    if a.max_inferred:
        row.notes.append("amount upper bound inferred from statutory bucket (page-break truncation)")
    if a.in_known_bucket is False:
        row.needs_review = True
        row.notes.append("amount range not a known statutory bucket")
    return a


def parse_ptr_row(row: Row) -> None:
    text = STITCH_RE.sub(r"\1 - \2 None", row.text)
    m = PTR_ANCHOR.search(text)
    if not m:
        row.needs_review = True
        row.notes.append("PTR row grammar not matched")
        return
    owner, asset_part = _split_owner_asset(text[: m.start()].strip())
    amt_raw = m.group("amt")
    tc = TRAILING_CODE.search(amt_raw)
    if tc and not CODE_RE.search(asset_part):
        asset_part = f"{asset_part} {tc.group(1)}"
        amt_raw = amt_raw[: tc.start()].strip()
        row.notes.append("asset type code recovered from amount cell "
                         "(page-layout wrap)")
    # Page break inside the row: the tail of the asset name (with its ticker and
    # type code) is stranded between the two halves of the amount range, e.g.
    #   "... P 01/16/2026 01/16/2026 $50,001 - Stock (TEM) [ST] $100,000"
    # Put the fragment back on the asset and rejoin the range.
    split = ASSET_IN_AMOUNT.match(amt_raw)
    if split:
        asset_part = f"{asset_part} {split.group('frag')}".strip()
        amt_raw = f"{split.group('lo')} {split.group('hi')}"
        row.notes.append("asset name reassembled across page break (ticker/type code "
                         "recovered from the amount cell)")
    amt = _amount(amt_raw, row, table="transaction")
    tx_type = m.group("t") + (" (partial)" if m.group("sub") else "")
    row.parsed = {
        "record": "transaction",
        "source_schedule": "PTR",
        "owner_code": owner,
        **_asset_fields(asset_part),
        "transaction_code": tx_type,
        "transaction_date": parse_us_date(m.group("d1")),
        "notification_date": parse_us_date(m.group("d2")),
        "amount": amt.as_dict(),
        "filing_status": row.filing_status,
        "description": row.description,
        "location": row.location,
        "comments": row.comments,
        "facts": parse_description(row.description).as_dict(),
        "needs_review": row.needs_review,
        "notes": row.notes,
    }


def parse_fdb_row(row: Row) -> None:
    text = STITCH_RE.sub(r"\1 - \2 None", row.text)
    code_m = CODE_RE.search(text)
    if code_m:
        asset_part, rest = text[: code_m.end()], text[code_m.end():].strip()
    else:
        asset_part, rest = "", text
    m = FDB_ANCHOR.search(rest)
    if not m:
        row.needs_review = True
        row.notes.append("FD Schedule B row grammar not matched")
        return
    asset_part = (asset_part + " " + rest[: m.start()]).strip()
    amt = _amount(m.group("amt"), row, table="transaction")
    tx_type = m.group("t") + (" (partial)" if m.group("sub") else "")
    row.parsed = {
        "record": "transaction",
        "source_schedule": "FD-B",
        "owner_code": m.group("owner"),
        **_asset_fields(asset_part),
        "transaction_code": tx_type,
        "transaction_date": parse_us_date(m.group("d1")),
        "notification_date": None,
        "amount": amt.as_dict(),
        "filing_status": None,
        "description": row.description,
        "location": row.location,
        "comments": row.comments,
        "facts": parse_description(row.description).as_dict(),
        "needs_review": row.needs_review,
        "notes": row.notes,
    }


def parse_fda_row(row: Row) -> None:
    text = STITCH_RE.sub(r"\1 - \2 None", row.text)
    if STITCH_RE.search(row.text):
        row.notes.append("value range re-stitched around page-break")
    code_m = CODE_RE.search(text)
    if not code_m:
        row.needs_review = True
        row.notes.append("FD Schedule A row has no asset-type code")
        return
    pre, post = text[: code_m.start()].strip(), text[code_m.end():].strip()
    disp = OWNER_OR_VALUE.search(pre)
    if disp:
        # pdftotext -layout wrapped the asset cell, so the type code landed
        # *after* the data columns. Reassemble: name is everything before the
        # first owner/value token; the data is that remainder plus whatever
        # followed the code.
        asset_part = f"{pre[: disp.start()].strip()} {code_m.group(0)}"
        rest = f"{pre[disp.start():]} {post}".strip()
        row.notes.append("row reassembled from page-layout wrap "
                         "(type code displaced by column wrapping)")
    else:
        asset_part, rest = text[: code_m.end()], post
    owner, rest = _split_owner_asset(rest)
    vm = VALUE_TOKEN.search(rest)
    if not vm:
        row.needs_review = True
        row.notes.append("FD Schedule A row has no value token")
        return
    value = _amount(vm.group(0), row)
    tail = rest[vm.end():].strip()

    # pdftotext -layout flattens the page grid, so a wrapped range's upper
    # bound can land *after* the income columns as a lone "$N". When the
    # value's max was bucket-inferred and a standalone fragment matches it,
    # consume it as confirmation; anything unexplained flags the row.
    def _consume_stray(txt: str, amount) -> str:
        if not (amount and amount.max_inferred and amount.max):
            return txt
        stray = re.search(
            rf"(?<![-\d]) ?\${amount.max:,}(?!\s*-)(?![\d,])", " " + txt)
        if stray:
            txt = (" " + txt)[: stray.start()] + (" " + txt)[stray.end():]
            amount.max_inferred = False
            row.notes.append("range upper bound confirmed from stranded "
                             "column fragment (page-layout wrap)")
        return txt.strip()

    # Collision repair: an income range's dangling "-" can capture the value's
    # stranded upper half ("Rent $100,001 - $25,000,000"). When the captured
    # pair is not a valid income bucket but its max matches the inferred value
    # max, split it back apart and take the max as confirmation.
    if value.max_inferred and value.max:
        coll = re.search(rf"(\$[\d,]+)\s*-\s*\${value.max:,}(?![\d,])", tail)
        if coll:
            lo = int(coll.group(1).replace("$", "").replace(",", ""))
            if INCOME_MIN_TO_MAX.get(lo) != value.max:
                tail = f"{tail[: coll.start()]}{coll.group(1)} - {tail[coll.end():]}".strip()
                value.max_inferred = False
                row.notes.append("range upper bound confirmed from stranded "
                                 "column fragment (page-layout wrap)")

    tail = _consume_stray(tail, value)
    income, income_types = None, None
    if tail:
        tokens = list(VALUE_TOKEN.finditer(tail))
        if tokens:
            last = tokens[-1]
            income = _amount(last.group(0), row, table="income")
            income_types = tail[: last.start()].strip().strip(",") or None
            trail = _consume_stray(tail[last.end():].strip(), income)
            trail = _consume_stray(trail, value)
            if trail:
                # wrapped income-type words trail the amount in -layout output
                if _is_income_type_words(trail) and income and not (
                        income.is_none or income.undetermined):
                    income_types = (f"{income_types} {trail}".strip()
                                    if income_types else trail)
                    row.notes.append("income type continuation reattached "
                                     "(page-layout wrap)")
                else:
                    row.needs_review = True
                    row.notes.append(f"unattached trailing text after income: {trail!r}")
            # value=None + income present: types text precedes the income token
        else:
            income_types = tail
    row.parsed = {
        "record": "position",
        "source_schedule": "FD-A",
        "owner_code": owner,
        **_asset_fields(asset_part),
        "value": value.as_dict(),
        "income_types": income_types,
        "income": income.as_dict() if income else None,
        "description": row.description,
        "location": row.location,
        "comments": row.comments,
        "facts": parse_description(row.description).as_dict(),
        "needs_review": row.needs_review,
        "notes": row.notes,
    }


_ROW_PARSERS = {"T": parse_ptr_row, "B": parse_fdb_row, "A": parse_fda_row}


# --------------------------------------------------------------------------
# document walk
# --------------------------------------------------------------------------

def _row_complete(section: str, text: str) -> bool:
    if section in ("T",):
        m = PTR_ANCHOR.search(text)
        return bool(m and AMT_COMPLETE.search(text))
    if section == "B":
        if not CODE_RE.search(text):
            return False
        m = FDB_ANCHOR.search(text)
        return bool(m and AMT_COMPLETE.search(text))
    if section == "A":
        code_m = CODE_RE.search(text)
        return bool(code_m and VALUE_TOKEN.search(text[code_m.end():]))
    return False


def _starts_new_row(section: str, buf_text: str, line: str) -> bool:
    """Content line arrives while a buffer exists: does it begin a new row?"""
    if not _row_complete(section, buf_text):
        return False
    if section == "A":
        return not A_TAIL.match(line)
    # T / B: amount continuations and a displaced lone "[XX]" type code
    # (pdftotext -layout puts a wrapped asset's code on its own line) extend
    # a complete row rather than starting a new one.
    return not (line.startswith("$") or line.startswith("(partial")
                or LONE_CODE.match(line))


def _desc_continues(label: str, text: str, line: str) -> bool:
    if label not in ("description", "comments"):
        return False
    if CITY_RE.fullmatch(text.strip()):
        return False
    if CODE_RE.search(line) or OWNER_LEAD.match(line):
        return False
    if not text.rstrip().endswith((".", "?", "!")):
        return True
    if TICKER_PARENS.search(line) or DATE_RE.search(line) or "$" in line:
        return False
    return True


def parse_filing_text(text: str, source: str | None = None) -> ParsedFiling:
    lines = tn.prepare_lines(text)
    head = " ".join(lines[:3])
    if "Periodic Transaction Report" in head or re.search(r"\bP\s*T\s*R\b", head):
        doc_type = "ptr"
    elif "Financial Disclosure Report" in head or re.search(r"\bF\s*D\s*R\b", head):
        doc_type = "fd"
    else:
        doc_type = "fd" if any(l.startswith("Filing Type:") for l in lines) else "ptr"

    pf = ParsedFiling(doc_type=doc_type, source=source)
    section = "T" if doc_type == "ptr" else None
    parse_rows = doc_type == "ptr"          # FD waits for Schedule A
    buf: list[str] = []
    last_row: Row | None = None
    active_label: str | None = None
    in_trailer = False
    order = 0

    def finalize():
        nonlocal buf, last_row, order
        if not buf:
            return
        row = Row(section=section or "?", text=" ".join(buf).strip(), order=order)
        order += 1
        buf = []
        if row.section in _ROW_PARSERS:
            pf.rows.append(row)
            last_row = row
        # rows outside A/B/T grammars are ignored here (captured in other_sections)

    for line in lines:
        fid = tn.FILING_ID_RE.match(line)
        if fid:
            if fid.group(1) not in pf.filing_ids:
                pf.filing_ids.append(fid.group(1))
            continue

        sm = tn.SIGNED_RE.search(line)
        if sm:
            pf.signed_by = sm.group("name").strip()
            pf.signed_date = parse_us_date(sm.group("date"))
            continue

        if in_trailer:
            continue

        if tn.TRAILER_RE.match(line):
            finalize()
            in_trailer = True
            active_label = None
            continue

        sec_m = SECTION_RE.match(line) if doc_type == "fd" else None
        if sec_m:
            finalize()
            section = sec_m.group(1).upper()
            parse_rows = section in ("A", "B")
            if not parse_rows:
                pf.other_sections.setdefault(section, [])
            active_label = None
            last_row = None
            continue

        if tn.is_furniture(line):
            continue

        meta_hit = False
        for key, rx in tn.META_RES.items():
            m = rx.match(line)
            if m and key not in pf.meta:
                pf.meta[key] = m.group(1).strip()
                meta_hit = True
                break
        if meta_hit:
            continue

        if not parse_rows:
            if section is not None:
                pf.other_sections.setdefault(section, []).append(line)
            continue

        lab = tn.match_label(line)
        if lab:
            name, value = lab
            finalize()
            if last_row is not None:
                current = getattr(last_row, name) or ""
                setattr(last_row, name, (current + " " + value).strip() if current else value)
                active_label = name
            continue

        if active_label and last_row is not None and \
                _desc_continues(active_label, getattr(last_row, active_label) or "", line):
            setattr(last_row, active_label,
                    ((getattr(last_row, active_label) or "") + " " + line).strip())
            continue
        active_label = None

        if buf and _starts_new_row(section, " ".join(buf), line):
            finalize()
            buf = [line]
        else:
            buf.append(line)

    finalize()
    # parse every row now that all sub-row labels are attached
    for r in pf.rows:
        if r.parsed is None:
            _ROW_PARSERS[r.section](r)
    return pf
