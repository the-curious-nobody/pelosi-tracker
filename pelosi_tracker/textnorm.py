"""Text normalization for House Clerk financial-disclosure PDFs.

Two extraction realities this layer absorbs:

1. Glyph cipher. The Clerk's e-filing templates render section/label text in a
   custom font whose lowercase letters map to Unicode IPA codepoints
   U+0283..U+029C ("Dʇʕʅʔʋʒʖʋʑʐ" == "Description"). We translate them back so
   labels are readable regardless of extractor.

2. Extractor variance. Some extractors drop the glyphs instead (yielding
   "D:", "F S:", "S B: T"). All downstream regexes therefore accept both the
   decoded long form and the stripped short form.
"""
from __future__ import annotations

import re

# --- glyph cipher -----------------------------------------------------------
_GLYPH_FIRST = 0x0283  # 'ʃ' == 'a'
_GLYPH_LAST = 0x029C   # 'ʜ' == 'z'
_GLYPH_MAP = {c: chr(ord("a") + (c - _GLYPH_FIRST)) for c in range(_GLYPH_FIRST, _GLYPH_LAST + 1)}


def decode_glyphs(text: str) -> str:
    """Translate the Clerk template's obfuscated lowercase glyphs back to a-z."""
    return text.translate(_GLYPH_MAP)


# --- generic cleanup --------------------------------------------------------
_WS = re.compile(r"[ \t\u00a0]+")
# checkbox/wingding junk seen in older (pre-2018) templates
_JUNK = re.compile(r"\b(?:gfedcb?|gfedc)\b")


def clean_line(line: str) -> str:
    line = decode_glyphs(line)
    line = _JUNK.sub(" ", line)
    line = _WS.sub(" ", line).strip()
    return line


def prepare_lines(text: str) -> list[str]:
    """Decode + clean; drop empty lines. Keeps original order."""
    out = []
    for raw in text.splitlines():
        line = clean_line(raw)
        if line:
            out.append(line)
    return out


# --- page furniture ---------------------------------------------------------
# Lines that are template chrome, repeated column headers at page breaks, etc.
# They may appear *inside* a logical row (page break mid-row) and must be
# skippable without breaking row assembly.
FURNITURE_PATTERNS = [
    r"^P\s*T\s*R$",
    r"^F\s*D\s*R$",
    r"^Periodic Transaction Report$",
    r"^Financial Disclosure Report$",
    r"Legislative Resource Center",
    r"^F\s*I$",
    r"^Filer Information$",
    r"^Filing Information$",
    r"^T$",
    r"^Transactions$",
    # PTR column headers
    r"^ID Owner Asset Transaction$",
    r"^Type$",
    r"^Date Notification$",
    r"^Date$",
    r"^Amount Cap\.$",
    r"^Gains >$",
    r"^\$200\?$",
    # FD Schedule A column headers
    r"^Asset Owner Value of Asset Income Type\(s\) Income Tx\. >$",
    r"^\$1,000\?$",
    # FD Schedule B column headers
    r"^Asset Owner Date Tx\.$",
    # FD Schedule D column headers
    r"^Owner Creditor Date Incurred Type Amount of$",
    r"^Liability$",
    r"^\* For the complete list of asset type abbreviations.*$",
    # pdftotext -layout can emit the multi-line column headers as ONE line;
    # match on distinctive header phrases no real row contains.
    r"^ID\s+Owner\s+Asset\b",
    r"Asset\s+Owner\s+Value of Asset",
    r"Income Type\(s\)",
    r"^Asset\s+Owner\s+Date\b",
    r"Gains\s*>\s*\$200\?",
    r"Tx\.\s*>\s*\$1,000\?",
    r"^Owner\s+Creditor\s+Date Incurred\b",
    r"^Type(?:\s+Date)?\s+Gains\s*>$",
    r"^Date\s+Gains\s*>$",
]
_FURNITURE = [re.compile(p) for p in FURNITURE_PATTERNS]

FILING_ID_RE = re.compile(r"^Filing ID\s*#\s*(\d+)\s*$")


def is_furniture(line: str) -> bool:
    return any(p.search(line) for p in _FURNITURE)


# --- labels -----------------------------------------------------------------
# Sub-row labels attached to the most recently completed row. Both the decoded
# long form and the glyph-stripped short form are accepted.
LABEL_RES = {
    "filing_status": re.compile(r"^(?:F\s*S|Filing\s*Status)\s*:\s*(.*)$", re.I),
    "description": re.compile(r"^(?:D|Description)\s*:\s*(.*)$"),
    "location": re.compile(r"^(?:L|Location)\s*:\s*(.*)$"),
    "comments": re.compile(r"^(?:C|Comments)\s*:\s*(.*)$"),
}


def match_label(line: str) -> tuple[str, str] | None:
    for name, rx in LABEL_RES.items():
        m = rx.match(line)
        if m:
            return name, m.group(1).strip()
    return None


# --- filing-level metadata --------------------------------------------------
META_RES = {
    "filer_name": re.compile(r"^Name:\s*(.+)$"),
    "filer_status": re.compile(r"^Status:\s*(.+)$"),
    "state_district": re.compile(r"^State/District:\s*(.+)$"),
    "filing_type": re.compile(r"^Filing Type:\s*(.+)$"),
    "filing_year": re.compile(r"^Filing Year:\s*(.+)$"),
    "filing_date": re.compile(r"^Filing Date:\s*(.+)$"),
}

SIGNED_RE = re.compile(r"Digitally Signed:\s*(?P<name>.+?)\s*,\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})")

# Sections after which no transaction rows can appear (PTR trailer / FD trailer).
TRAILER_RE = re.compile(
    r"^(?:I\s*P\s*O$|IPO:|C\s+S$|Certification|I CERTIFY|Yes No$|"
    r"E\s+S, D,\s+T I$|Trusts:|Exemption:|"
    # glyph-decoded long forms as raw pdftotext renders them
    r"Initial Public Offering|Exclusions of Spouse)"
)
