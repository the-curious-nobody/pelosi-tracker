"""Parse the free-text D: description lines.

The filings encode the economically meaningful details (option contracts,
strike, expiration, exact share counts, exercises, charitable gifts) only in
prose. A row coded "S (partial)" can be an open-market sale *or* a donation;
a row coded "P" can be a fresh option purchase *or* an exercise converting old
options into stock. Flattening these to buy/sell (as commercial trackers do)
destroys information — so we don't.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date


def parse_us_date(s: str) -> str | None:
    """M/D/YY or M/D/YYYY -> ISO string. 2-digit years assumed 20xx."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s.strip())
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


_LOT = re.compile(
    r"([\d,]+)\s+(call|put)\s+options?\s+with\s+a\s+strike\s+price\s+of\s+"
    r"\$([\d,.]+)\s+and\s+an\s+expiration\s+date\s+of\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)
_EXERCISE = re.compile(
    r"Exercised\s+([\d,]+)\s+(call|put)\s+options?\s+purchased\s+"
    r"((?:\d{1,2}/\d{1,2}/\d{2,4})(?:\s*&\s*\d{1,2}/\d{1,2}/\d{2,4})*)\s*"
    r"\(\s*([\d,]+)\s+shares\s*\)\s*at\s+a\s+strike\s+price\s+of\s+\$([\d,.]+)"
    r"(?:\s+with\s+an\s+expiration\s+date\s*of\s+(\d{1,2}/\d{1,2}/\d{2,4}))?",
    re.I,
)
_SHARES = re.compile(r"\b(?:Sold|Sale of|Contribution of|Purchased)\s+([\d,]+)\s+(shares|units)\b", re.I)
_RECEIVED = re.compile(r"([\d,]+)\s+(shares|units)\b[^.]*\breceived\b", re.I)
_GIFT = re.compile(r"\bcontribution\b|\bdonor-?advised\b|\bdonat", re.I)
_PURCHASED_LOT = re.compile(r"\bPurchased\b", re.I)


def _n(s: str) -> int:
    return int(s.replace(",", ""))


@dataclass
class OptionLot:
    contracts: int
    option_type: str          # call | put
    strike: float
    expiration: str | None    # ISO

    def as_dict(self) -> dict:
        return {"contracts": self.contracts, "option_type": self.option_type,
                "strike": self.strike, "expiration": self.expiration}


@dataclass
class DescriptionFacts:
    kind: str = "other"                 # option_purchase | option_position |
                                        # exercise | share_sale | unit_sale |
                                        # gift | other | none
    lots: list[OptionLot] = field(default_factory=list)
    shares: int | None = None
    units: int | None = None
    is_gift: bool = False
    exercise: dict | None = None        # contracts/type/strike/expiration/purchase_dates/shares

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "lots": [l.as_dict() for l in self.lots],
            "shares": self.shares,
            "units": self.units,
            "is_gift": self.is_gift,
            "exercise": self.exercise,
        }


def parse_description(text: str | None) -> DescriptionFacts:
    if not text:
        return DescriptionFacts(kind="none")
    f = DescriptionFacts()

    ex = _EXERCISE.search(text)
    if ex:
        f.kind = "exercise"
        f.exercise = {
            "contracts": _n(ex.group(1)),
            "option_type": ex.group(2).lower(),
            "purchase_dates": [parse_us_date(d) for d in re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", ex.group(3))],
            "shares": _n(ex.group(4)),
            "strike": float(ex.group(5).replace(",", "")),
            "expiration": parse_us_date(ex.group(6)) if ex.group(6) else None,
        }
        f.shares = f.exercise["shares"]
        return f

    for m in _LOT.finditer(text):
        f.lots.append(OptionLot(
            contracts=_n(m.group(1)),
            option_type=m.group(2).lower(),
            strike=float(m.group(3).replace(",", "")),
            expiration=parse_us_date(m.group(4)),
        ))
    if f.lots:
        f.kind = "option_purchase" if _PURCHASED_LOT.search(text) else "option_position"

    sm = _SHARES.search(text)
    if sm:
        n, unit = _n(sm.group(1)), sm.group(2).lower()
        if unit == "shares":
            f.shares = n
        else:
            f.units = n
        if not f.lots:
            if re.search(r"\bSold\b|\bSale of\b", text, re.I):
                f.kind = "share_sale" if unit == "shares" else "unit_sale"
            elif re.search(r"\bPurchased\b", text, re.I):
                f.kind = "share_purchase" if unit == "shares" else "unit_purchase"
    else:
        rm = _RECEIVED.search(text)
        if rm:
            n, unit = _n(rm.group(1)), rm.group(2).lower()
            if unit == "shares":
                f.shares = n
            else:
                f.units = n
            if not f.lots:
                f.kind = "shares_received"

    if _GIFT.search(text):
        f.is_gift = True
        if not f.lots and f.exercise is None:
            f.kind = "gift"
    if f.kind == "other" and (f.shares or f.units):
        f.kind = "share_transaction"
    return f
