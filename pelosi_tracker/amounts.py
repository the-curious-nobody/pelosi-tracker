"""Disclosure amount ranges.

Values are disclosed only as statutory buckets. Because the buckets are fixed,
a truncated range (page break ate the upper bound) can be repaired: the lower
bound uniquely determines the bucket. Repaired values are flagged, never
silently asserted (Product Principle: clearly labeled estimates).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Transaction / asset-value buckets (Ethics in Government Act ranges as used on
# House PTR & FD forms).
KNOWN_BUCKETS: list[tuple[int, int | None]] = [
    (1, 1_000),
    (1_001, 15_000),
    (15_001, 50_000),
    (50_001, 100_000),
    (100_001, 250_000),
    (250_001, 500_000),
    (500_001, 1_000_000),
    (1_000_001, 5_000_000),
    (5_000_001, 25_000_000),
    (25_000_001, 50_000_000),
    (50_000_000, None),  # "Over $50,000,000"
]
_MIN_TO_MAX = {lo: hi for lo, hi in KNOWN_BUCKETS}

# "Unearned" income tiers on FD Schedule A differ from asset/transaction buckets.
KNOWN_INCOME_BUCKETS: list[tuple[int, int | None]] = [
    (1, 200),
    (201, 1_000),
    (1_001, 2_500),
    (2_501, 5_000),
    (5_001, 15_000),
    (15_001, 50_000),
    (50_001, 100_000),
    (100_001, 1_000_000),
    (1_000_001, 5_000_000),
    (5_000_000, None),  # "Over $5,000,000"
]
INCOME_MIN_TO_MAX = {lo: hi for lo, hi in KNOWN_INCOME_BUCKETS}
_TABLES = {
    "asset": _MIN_TO_MAX,
    "transaction": _MIN_TO_MAX,
    "income": INCOME_MIN_TO_MAX,
}

_NUM = re.compile(r"\$([\d,]+)")

RANGE_RE = re.compile(
    r"(?:\$[\d,]+\s*-\s*\$[\d,]+"      # $A - $B
    r"|Over\s+\$[\d,]+"                # Over $A
    r"|None\b"
    r"|Undetermined\b)"
)
DANGLING_RE = re.compile(r"\$[\d,]+\s*-\s*$")


def _to_int(s: str) -> int:
    return int(s.replace(",", ""))


@dataclass
class AmountRange:
    raw: str
    min: int | None = None
    max: int | None = None
    is_none: bool = False
    undetermined: bool = False
    max_inferred: bool = False        # upper bound repaired from bucket table
    in_known_bucket: bool | None = None
    exact: bool = False               # filing disclosed a precise figure, not a range

    @classmethod
    def parse(cls, raw: str, table: str = "asset") -> "AmountRange":
        buckets = _TABLES.get(table, _MIN_TO_MAX)
        raw = raw.strip()
        if raw.lower().startswith("none"):
            return cls(raw=raw, is_none=True)
        if raw.lower().startswith("undetermined"):
            return cls(raw=raw, undetermined=True)
        if raw.lower().startswith("over"):
            nums = _NUM.findall(raw)
            lo = _to_int(nums[0]) if nums else None
            return cls(raw=raw, min=lo, max=None, in_known_bucket=lo in buckets)
        nums = _NUM.findall(raw)
        if len(nums) >= 2:
            lo, hi = _to_int(nums[0]), _to_int(nums[1])
            return cls(raw=raw, min=lo, max=hi,
                       in_known_bucket=buckets.get(lo, object()) == hi)
        if len(nums) == 1:
            lo = _to_int(nums[0])
            if not DANGLING_RE.search(raw):
                # A precise figure the filer actually disclosed (e.g. "$15.00"
                # cash-in-lieu from a spinoff). Not a bucket, not truncated:
                # report it as filed rather than inventing an upper bound.
                return cls(raw=raw, min=lo, max=lo, exact=True, in_known_bucket=None)
            # dangling "$X -" : repair from bucket table, flagged
            hi = buckets.get(lo)
            return cls(raw=raw, min=lo, max=hi, max_inferred=hi is not None,
                       in_known_bucket=lo in buckets)
        return cls(raw=raw)

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "min": self.min,
            "max": self.max,
            "is_none": self.is_none,
            "undetermined": self.undetermined,
            "max_inferred": self.max_inferred,
            "in_known_bucket": self.in_known_bucket,
            "exact": self.exact,
        }
