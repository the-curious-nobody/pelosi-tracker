"""Estimated-holdings reconstruction.

Design constraints (Product Principles):
  - Confirmed transactions and estimated holdings are separate record types.
  - No share balances are computed unless share counts were actually disclosed;
    we report *disclosed movements* instead of inventing a balance.
  - Every status carries a confidence tier and the evidence behind it.
  - Staleness degrades confidence explicitly (disclosures lag reality).

Inputs are the parsed annual Schedule A (baseline "reported held at period
end") plus all transactions (PTR + annual Schedule B), deduplicated across
sources: the same trade legally appears in both a PTR and the annual report.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

STALE_AFTER_DAYS = 90


# --------------------------------------------------------------------------
# cross-source transaction dedupe
# --------------------------------------------------------------------------

def _tx_key(t: dict) -> tuple:
    # asset_type_code matters: on 2026-07-24 the same underlying (BE) was bought
    # as stock [ST] and as calls [OP] on the same day in the same amount bucket.
    # Without it those collapse into one record and a real trade disappears.
    a = t["amount"]
    return (t.get("transaction_date"), t.get("asset_id"),
            t.get("asset_type_code"),
            (t.get("transaction_code") or "").replace(" ", ""),
            a.get("min"), t.get("owner_code"))


def annotate_disclosure_delay(txs: list[dict]) -> list[dict]:
    """Attach disclosure delay to merged transactions, in place.

    The delay is measured against the earliest ingested disclosure of the
    trade. When a trade is known only from an annual report, an earlier
    periodic report almost certainly exists but isn't ingested, so the figure
    is an upper bound and is labelled as one - never presented as the STOCK
    Act lag.
    """
    for t in txs:
        if not (t.get("transaction_date") and t.get("filed_date")):
            continue
        t["disclosure_delay_days"] = (
            date.fromisoformat(t["filed_date"])
            - date.fromisoformat(t["transaction_date"])).days
        t["delay_basis"] = ("ptr" if t.get("source_schedule") == "PTR"
                            else "annual_report_upper_bound")
    return txs


def dedupe_transactions(txs: list[dict]) -> list[dict]:
    """Merge records describing the same trade. PTR record wins as the base
    (it carries notification date and filing status); other sources contribute
    provenance and any fields the base lacks (e.g. Schedule B comments)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in txs:
        groups[_tx_key(t)].append(t)
    merged = []
    for key, group in groups.items():
        group.sort(key=lambda t: 0 if t["source_schedule"] == "PTR" else 1)
        base = dict(group[0])
        base["provenance"] = []
        for t in group:
            base["provenance"].append({
                "filing_id": t.get("filing_id"),
                "schedule": t.get("source_schedule"),
                "source_url": t.get("source_url"),
            })
            for f in ("comments", "location", "description", "notification_date"):
                if not base.get(f) and t.get(f):
                    base[f] = t[f]
        base["corroborated"] = len(group) > 1
        merged.append(base)
    merged.sort(key=lambda t: (t.get("transaction_date") or "", t.get("asset_id") or ""))
    return merged


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _confidence_with_staleness(conf: str, latest_evidence: str | None,
                               as_of: date, notes: list[str],
                               coverage_through: str | None = None,
                               known_gaps: list[str] | None = None) -> str:
    """Degrade stale confidence, describing exactly what the record covers.

    "No later disclosures" is only meaningful relative to what has actually
    been ingested - so the note states the coverage horizon and any known
    un-ingested filings instead of guessing why the evidence is old.
    """
    if latest_evidence:
        gap = (as_of - date.fromisoformat(latest_evidence)).days
        if gap > STALE_AFTER_DAYS and conf == "high":
            msg = (f"no disclosures for this asset after {latest_evidence} "
                   f"({gap} days before as-of date)")
            if coverage_through:
                msg += f" in filings ingested through {coverage_through}"
            if known_gaps:
                msg += (f"; caution: {len(known_gaps)} known filing(s) not ingested "
                        f"({', '.join(known_gaps)})")
            notes.append(msg)
            return "medium"
    return conf


def _movement(t: dict) -> dict | None:
    f = t["facts"]
    n = f.get("shares") or f.get("units")
    if not n:
        return None
    code = t["transaction_code"]
    sign = -1 if code.startswith("S") else 1
    return {
        "date": t["transaction_date"],
        "shares": sign * n,
        "unit": "units" if f.get("units") else "shares",
        "kind": f["kind"],
        "is_gift": f.get("is_gift", False),
        "filing_ids": [p["filing_id"] for p in t.get("provenance", [])],
    }


# --------------------------------------------------------------------------
# reconstruction
# --------------------------------------------------------------------------

def reconstruct(positions: list[dict], transactions: list[dict],
                as_of: date, period_end: date,
                registry: dict[str, dict],
                known_gaps: list[str] | None = None) -> dict:
    period_end_iso = period_end.isoformat()
    known_gaps = known_gaps or []
    coverage_through = max((t.get("filed_date") or "" for t in transactions),
                           default="") or None
    tx_by_asset: dict[str, list[dict]] = defaultdict(list)
    for t in transactions:
        tx_by_asset[t["asset_id"]].append(t)

    stocks, options, other_assets = [], [], []
    seen_stock_ids: set[str] = set()

    # ---- option-lot ledger ------------------------------------------------
    lots: dict[tuple, dict] = {}

    def lot_key(aid, typ, strike, exp):
        return (aid, typ, strike, exp)

    for p in positions:
        if p["instrument"] != "option":
            continue
        for lot in p["facts"]["lots"]:
            k = lot_key(p["asset_id"], lot["option_type"], lot["strike"], lot["expiration"])
            e = lots.setdefault(k, {"asset_id": p["asset_id"], **lot,
                                    "evidence": [], "acquired": None, "resolution": None})
            e["snapshot_contracts"] = lot["contracts"]
            e["evidence"].append({"type": "annual_position", "filing_id": p.get("filing_id"),
                                  "as_of_period_end": period_end_iso,
                                  "value_range": p["value"]})
    for t in transactions:
        f = t["facts"]
        if f["kind"] == "option_purchase":
            for lot in f["lots"]:
                k = lot_key(t["asset_id"], lot["option_type"], lot["strike"], lot["expiration"])
                e = lots.setdefault(k, {"asset_id": t["asset_id"], **lot,
                                        "evidence": [], "acquired": None, "resolution": None})
                # An annual position is a snapshot that already reflects every
                # purchase up to the period end, so only purchases *after* the
                # baseline add contracts. Identical lots bought on separate
                # dates (BE: 100 calls on 7/24 + 100 on 7/28) accumulate.
                if (t["transaction_date"] or "") > period_end_iso:
                    e["added_contracts"] = e.get("added_contracts", 0) + lot["contracts"]
                d = t["transaction_date"]
                e["acquired"] = min(e["acquired"], d) if e["acquired"] and d else (d or e["acquired"])
                e["evidence"].append({"type": "purchase", "filing_id": t.get("filing_id"),
                                      "date": t["transaction_date"], "amount": t["amount"]})
        elif f["kind"] == "exercise":
            ex = f["exercise"]
            k = lot_key(t["asset_id"], ex["option_type"], ex["strike"], ex["expiration"])
            e = lots.get(k)
            if e is None and ex["expiration"] is None:
                for kk, vv in lots.items():
                    if kk[:3] == (t["asset_id"], ex["option_type"], ex["strike"]):
                        e = vv
                        break
            if e is None:
                e = lots.setdefault(k, {"asset_id": t["asset_id"],
                                        "contracts": ex["contracts"],
                                        "option_type": ex["option_type"],
                                        "strike": ex["strike"],
                                        "expiration": ex["expiration"],
                                        "evidence": [], "acquired": None, "resolution": None,
                                        "notes": ["acquisition predates ingested filings"]})
                if ex.get("purchase_dates"):
                    e["acquired"] = ex["purchase_dates"][0]
            e["resolution"] = {"kind": "exercised", "date": t["transaction_date"],
                               "shares": ex["shares"], "filing_id": t.get("filing_id")}
        elif t["instrument"] == "option" and t["transaction_code"].startswith("S"):
            for lot in f.get("lots", []):
                k = lot_key(t["asset_id"], lot["option_type"], lot["strike"], lot["expiration"])
                if k in lots:
                    lots[k]["resolution"] = {"kind": "sold", "date": t["transaction_date"],
                                             "filing_id": t.get("filing_id")}

    for k, e in sorted(lots.items(), key=lambda kv: (kv[0][0], kv[0][3] or "")):
        notes = list(e.get("notes", []))
        snap, added = e.get("snapshot_contracts"), e.get("added_contracts", 0)
        if snap is not None:
            e["contracts"] = snap + added
        elif added:
            e["contracts"] = added
        buys = [ev for ev in e["evidence"] if ev.get("type") == "purchase"]
        if added and len(buys) > 1:
            notes.append(f"{len(buys)} disclosed purchases of this lot combined "
                         f"({', '.join(b['date'] for b in buys if b.get('date'))})")
        if e["resolution"]:
            r = e["resolution"]
            status = f"{r['kind'].capitalize()} on {r['date']}"
            if r["kind"] == "exercised":
                status += f" -> {r['shares']:,} shares"
            conf = "high"
        elif e["expiration"] and date.fromisoformat(e["expiration"]) < as_of:
            status = (f"Reached expiration {e['expiration']}; outcome (exercise vs expiry) "
                      "not present in ingested filings - check subsequent PTRs")
            conf = "low"
        else:
            status = "Likely Held"
            latest = max((ev.get("date") or ev.get("as_of_period_end") or ""
                          for ev in e["evidence"]), default=None)
            conf = _confidence_with_staleness("high", latest or None, as_of, notes,
                                              coverage_through, known_gaps)
        reg = registry.get(e["asset_id"], {})
        options.append({
            "asset_id": e["asset_id"],
            "name": reg.get("canonical_name", e["asset_id"]),
            "option_type": e["option_type"], "contracts": e["contracts"],
            "strike": e["strike"], "expiration": e["expiration"],
            "acquired": e["acquired"], "status": status, "confidence": conf,
            "evidence": e["evidence"], "resolution": e["resolution"], "notes": notes,
        })

    # ---- stock / security positions --------------------------------------
    for p in positions:
        aid = p["asset_id"]
        if not (registry.get(aid, {}).get("is_security")) or p["instrument"] == "option":
            if p["instrument"] != "option":
                other_assets.append({
                    "asset_id": aid, "name": p["asset_name"], "owner": p["owner_code"],
                    "instrument": p["instrument"], "value_range": p["value"],
                    "income_types": p["income_types"], "income": p["income"],
                    "status": f"Reported in annual report (period ending {period_end_iso})",
                    "location": p.get("location"), "description": p.get("description"),
                })
            continue
        seen_stock_ids.add(aid)
        notes: list[str] = []
        post = [t for t in tx_by_asset.get(aid, [])
                if t["instrument"] != "option"
                and (t["transaction_date"] or "") > period_end_iso]
        movements = [m for m in (_movement(t) for t in tx_by_asset.get(aid, [])
                                 if t["instrument"] != "option") if m]
        evidence = [{"type": "annual_position", "filing_id": p.get("filing_id"),
                     "as_of_period_end": period_end_iso, "value_range": p["value"]}]
        latest = period_end_iso
        if p["value"]["is_none"]:
            status, conf = f"Closed (as of {period_end_iso})", "high"
            notes.append("annual report lists the position with no year-end value; "
                         "income column shows it existed during the year")
        else:
            status, conf = "Likely Held", "high"
        for t in sorted(post, key=lambda t: t["transaction_date"]):
            evidence.append({"type": "transaction", "filing_id": t.get("filing_id"),
                             "date": t["transaction_date"], "code": t["transaction_code"]})
            latest = max(latest, t["transaction_date"])
            code = t["transaction_code"]
            if code == "S":
                status, conf = f"Closed (full sale {t['transaction_date']})", "high"
            elif code.startswith("S"):
                status, conf = "Likely Held - Reduced", "medium"
            elif code.startswith("P"):
                status = "Likely Held - Increased" if status.startswith("Likely") else status
                conf = "medium" if conf != "high" else conf
        if status.startswith("Likely"):
            conf = _confidence_with_staleness(conf, latest, as_of, notes,
                                              coverage_through, known_gaps)
        reg = registry.get(aid, {})
        stocks.append({
            "asset_id": aid, "ticker": p.get("ticker"),
            "name": reg.get("canonical_name", p["asset_name"]),
            "owner": p["owner_code"], "instrument": p["instrument"],
            "period_end_value": p["value"], "income_types": p["income_types"],
            "income": p["income"], "status": status, "confidence": conf,
            "disclosed_movements": movements, "evidence": evidence, "notes": notes,
        })

    # ---- assets that appear only in transactions --------------------------
    other_by_id = {o["asset_id"]: o for o in other_assets}
    for aid, txs in tx_by_asset.items():
        if aid in seen_stock_ids:
            continue
        stock_txs = [t for t in txs if t["instrument"] != "option"]
        if not stock_txs:
            continue
        reg = registry.get(aid, {})

        # Non-securities (LLC/LP follow-ons, property) never belong in the
        # securities table. Annotate their annual entry instead - or create a
        # transactions-only stub so the disclosure isn't dropped.
        if not reg.get("is_security"):
            oa = other_by_id.get(aid)
            tx_note = "; ".join(
                f"{'disposition' if t['transaction_code'].startswith('S') else 'follow-on investment'} "
                f"disclosed {t['transaction_date']} (filing {t.get('filing_id')})"
                for t in sorted(stock_txs, key=lambda t: t["transaction_date"] or ""))
            if oa is not None:
                oa.setdefault("notes", []).append(tx_note)
                oa["status"] += "; subsequent transaction disclosed"
            else:
                other_assets.append({
                    "asset_id": aid, "name": reg.get("canonical_name", aid),
                    "owner": stock_txs[0].get("owner_code"),
                    "instrument": stock_txs[0]["instrument"], "value_range": None,
                    "income_types": None, "income": None,
                    "status": "Disclosed in transactions only (no annual baseline ingested)",
                    "location": None, "description": None, "notes": [tx_note],
                })
            continue

        last = max(stock_txs, key=lambda t: t["transaction_date"] or "")
        code = last["transaction_code"]
        # A same-asset annual position under a non-security code (AB is listed
        # as an [OL] LP interest) IS a baseline - say so, and cross-reference,
        # rather than shipping two contradictory rows for one asset.
        annual_other = other_by_id.get(aid)
        notes = ["no annual-report baseline ingested for this asset"]
        if annual_other is not None:
            notes = [f"annual report lists this holding under a non-security type "
                     f"({annual_other['instrument']}); see other reported assets "
                     f"for its {period_end_iso} value range"]
            annual_other.setdefault("notes", []).append(
                "subsequent securities transaction disclosed - see securities table")
            if code == "S":
                status, conf = f"Closed (full sale {last['transaction_date']})", "medium"
            elif code.startswith("S"):
                status, conf = "Partially Sold", "medium"
            else:
                status, conf = "Likely Held - Increased", "medium"
        elif code == "S":
            status, conf = f"Closed (full sale {last['transaction_date']})", "medium"
        elif code.startswith("S"):
            status, conf = "Possibly Held (partial sale observed; no annual baseline)", "low"
        elif code.startswith("E"):
            status, conf = ("Possibly Held (shares received in corporate action; "
                            "no annual baseline)", "low")
        else:
            status, conf = "Possibly Held (purchase observed; no annual baseline)", "low"
        stocks.append({
            "asset_id": aid, "ticker": stock_txs[0].get("ticker"),
            "name": reg.get("canonical_name", aid), "owner": stock_txs[0].get("owner_code"),
            "instrument": stock_txs[0]["instrument"],
            "period_end_value": annual_other["value_range"] if annual_other else None,
            "income_types": None, "income": None, "status": status, "confidence": conf,
            "disclosed_movements": [m for m in (_movement(t) for t in stock_txs) if m],
            "evidence": [{"type": "transaction", "filing_id": t.get("filing_id"),
                          "date": t["transaction_date"], "code": t["transaction_code"]}
                         for t in stock_txs],
            "notes": notes,
        })

    stocks.sort(key=lambda s: (s["ticker"] or "~", s["asset_id"]))
    return {
        "as_of": as_of.isoformat(),
        "baseline_period_end": period_end_iso,
        "coverage": {"filed_through": coverage_through, "known_gaps": known_gaps},
        "method": ("annual Schedule A baseline + subsequent transactions; "
                   "share balances are never computed - only disclosed movements are shown"),
        "stocks": stocks,
        "options": options,
        "other_assets": other_assets,
    }
