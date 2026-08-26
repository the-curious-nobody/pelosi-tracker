"""Canonical asset registry.

Different filings describe the same security differently ("NVIDIA Corporation -
Common Stock", "NVIDIA Corp"). We resolve every record to a canonical asset id:
the ticker when one is disclosed, otherwise a slug of the cleaned name. Tickers
come only from the filing's own parenthetical, never guessed (Product
Principle: never infer a ticker at low confidence without marking it).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# Observed mapping of the Clerk's asset-type codes. Non-authoritative; the
# official list lives at https://fd.house.gov/reference/asset-type-codes.aspx
# and unknown codes are surfaced rather than swallowed.
CODE_MAP = {
    "ST": "stock",
    "OP": "option",
    "MF": "mutual_fund",
    "OT": "other_security",
    "PS": "stock_non_public",
    "BA": "bank_account",
    "RP": "real_property",
    "OL": "business_interest",
    "AB": "asset_backed_or_fund",
    "IP": "intellectual_property",
}
SECURITY_CODES = {"ST", "OP", "MF", "OT", "PS"}

_SUFFIX = re.compile(
    r"\s*[-–]?\s*(?:Common Stock|Class [A-Z](?:\s+Common\s+Stock)?|Series [A-Z])\s*$",
    re.I,
)
_PARENS_TICKER = re.compile(r"\s*\(([A-Z][A-Z0-9.&]{0,6})\)\s*")


def clean_issuer_name(name: str) -> str:
    n = _PARENS_TICKER.sub(" ", name)
    n = re.sub(r"\s+", " ", n).strip().rstrip(",")
    prev = None
    while prev != n:
        prev = n
        n = _SUFFIX.sub("", n).strip().rstrip(",")
    return n


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def asset_key(record: dict) -> str:
    if record.get("ticker"):
        return record["ticker"]
    return slugify(clean_issuer_name(record.get("asset_name", "")) or "unknown")


def instrument(record: dict) -> str:
    return CODE_MAP.get(record.get("asset_type_code") or "", "unknown")


def annotate(records: list[dict]) -> None:
    """Attach asset_id / instrument / underlying_ticker in place."""
    for r in records:
        r["asset_id"] = asset_key(r)
        r["instrument"] = instrument(r)
        if r.get("asset_type_code") not in CODE_MAP and r.get("asset_type_code"):
            r.setdefault("notes", []).append(
                f"unrecognized asset-type code [{r['asset_type_code']}]")
        r["underlying_ticker"] = r.get("ticker") if r["instrument"] == "option" else None


def build_registry(records: list[dict]) -> dict[str, dict]:
    names: dict[str, Counter] = defaultdict(Counter)
    info: dict[str, dict] = {}
    for r in records:
        aid = r["asset_id"]
        cleaned = clean_issuer_name(r.get("asset_name", "")) or aid
        names[aid][cleaned] += 1
        entry = info.setdefault(aid, {
            "asset_id": aid,
            "ticker": r.get("ticker"),
            "is_security": False,
            "instruments": set(),
            "name_variants": set(),
        })
        entry["instruments"].add(r["instrument"])
        entry["name_variants"].add(r.get("asset_name", "").strip())
        if r.get("asset_type_code") in SECURITY_CODES:
            entry["is_security"] = True
    for aid, entry in info.items():
        entry["canonical_name"] = names[aid].most_common(1)[0][0]
        entry["instruments"] = sorted(entry["instruments"])
        entry["name_variants"] = sorted(entry["name_variants"])
    return info
