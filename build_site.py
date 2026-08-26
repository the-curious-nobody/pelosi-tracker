"""Build the static site from pipeline outputs.

Reads out/{transactions,holdings,assets}.json (+ filings from out/pelosi.db)
and writes self-contained HTML pages to site/. No frameworks, no build step,
no external assets except Google Fonts (with local fallbacks): the pages
deploy anywhere and rebuild in milliseconds after each ingest.

Design system ("ledger" direction):
  paper #F4F6F1 / ink #1B241F / ledger green #1E5B44 (acquisitions, links)
  oxide #8C3A2B (dispositions) / amber #8F6A12 (caution, medium confidence)
  Libre Caslon Text (display) · IBM Plex Sans (body) · IBM Plex Mono (data)
Signature element: every dollar figure renders as a log-scaled statutory
range bar — the form's buckets drawn literally, never a point estimate.
"""
from __future__ import annotations

import html
import json
import os
import math
import sqlite3
import statistics
from datetime import date, datetime, timezone
from pathlib import Path

from pelosi_tracker.amounts import KNOWN_BUCKETS

ROOT = Path(__file__).parent
OUT = ROOT / "out"
SITE = ROOT / "site"

LOG_TOP = math.log10(50_000_000)
VERSION = "0.1.0"


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def money(n: int) -> str:
    return "$" + format(n, ",")


def fmt_date(iso: str | None, year: bool = True) -> str:
    if not iso:
        return "—"
    d = date.fromisoformat(iso)
    return d.strftime("%b %-d, %Y") if year else d.strftime("%b %-d")


# ---------------------------------------------------------------- amounts ---
def amount_html(a: dict, tone: str = "hold") -> str:
    """Range bar + label. tone: buy | sell | hold."""
    if not a or a.get("is_none"):
        return '<span class="nil">—</span>'
    if a.get("undetermined"):
        return '<span class="nil">Undetermined</span>'
    lo = a.get("min")
    if lo is None:
        return f'<span class="nil">{esc(a.get("raw", ""))}</span>'
    if a.get("exact"):
        # The filer disclosed a precise figure (cash in lieu on a spinoff, say),
        # not a statutory bucket. Show a tick, not a span.
        left = max(0.0, math.log10(max(lo, 1)) / LOG_TOP * 100)
        return (
            f'<div class="amt" data-v="{lo}">'
            f'<div class="bar"><i class="exact" style="left:{left:.1f}%"></i></div>'
            f'<span class="amt-label">{esc(a.get("raw", money(lo)))} '
            f'<span class="exact-tag" title="Disclosed as an exact figure on the filing, '
            f'not a statutory range.">exact</span></span></div>'
        )
    hi = a.get("max")
    left = max(0.0, math.log10(max(lo, 1)) / LOG_TOP * 100)
    if hi:
        width = max(2.5, math.log10(hi) / LOG_TOP * 100 - left)
        label = f"{money(lo)}–{money(hi)}"
        open_cls = ""
    else:
        width = max(2.5, 100 - left)
        label = f"{money(lo)}+"
        open_cls = " open"
    star = '<sup class="inf" title="Upper bound inferred from the statutory bucket table (a page break truncated the filed range). Flagged, never silent.">*</sup>' if a.get("max_inferred") else ""
    sort_v = lo
    return (
        f'<div class="amt" data-v="{sort_v}">'
        f'<div class="bar"><i class="{tone}{open_cls}" style="left:{left:.1f}%;width:{width:.1f}%"></i></div>'
        f'<span class="amt-label">{label}{star}</span></div>'
    )


# ----------------------------------------------------------------- chips ----
def action_of(t: dict) -> tuple[str, str]:
    facts = t.get("facts") or {}
    code = t.get("transaction_code") or ""
    if facts.get("kind") == "exercise":
        return "Exercise", "act-ex"
    if facts.get("is_gift"):
        return "Gift", "act-gift"
    if code.startswith("P"):
        return "Purchase", "act-buy"
    if code.startswith("S"):
        return ("Sale · partial", "act-sell") if "partial" in code else ("Sale", "act-sell")
    if code.startswith("E"):
        return "Exchange", "act-ex"
    return code or "?", "act-ex"


def details_of(t: dict) -> str:
    f = t.get("facts") or {}
    bits = []
    if f.get("exercise"):
        e = f["exercise"]
        pd = ", ".join(fmt_date(d) for d in e.get("purchase_dates") or [] if d)
        bits.append(
            f'{e["contracts"]:,} {e["option_type"]}s @ {money(int(e["strike"]))} '
            f'→ {e["shares"]:,} sh' + (f' <span class="mut">(bought {pd})</span>' if pd else "")
        )
    for lot in f.get("lots") or []:
        exp = fmt_date(lot.get("expiration"))
        bits.append(f'{lot["contracts"]:,} {lot["option_type"]}s @ {money(int(lot["strike"]))} · exp {exp}')
    if not f.get("exercise") and f.get("shares"):
        bits.append(f'{f["shares"]:,} shares')
    if f.get("units"):
        bits.append(f'{f["units"]:,} units')
    txt = " · ".join(bits) if bits else '<span class="mut">—</span>'
    desc = t.get("description")
    return f'<span title="{esc(desc)}">{txt}</span>' if desc else txt


def delay_html(t: dict) -> str:
    d = t.get("disclosure_delay_days")
    if d is None:
        return '<td class="num" data-v="99999">—</td>'
    if t.get("delay_basis") == "annual_report_upper_bound":
        tip = ("Upper bound: measured to the annual report that disclosed this "
               "transaction. A periodic report covering it likely exists but is "
               "not yet ingested.")
        return f'<td class="num" data-v="{d}">{d}d<sup class="ub" title="{esc(tip)}">^</sup></td>'
    return f'<td class="num" data-v="{d}">{d}d</td>'


def source_chips(t: dict) -> str:
    chips = []
    for p in t.get("provenance") or []:
        lbl = "PTR" if p.get("schedule") == "PTR" else "FD-B"
        chips.append(
            f'<a class="src" href="{esc(p.get("source_url"))}" target="_blank" '
            f'rel="noopener" title="Filing {esc(p.get("filing_id"))} — official PDF, Clerk of the House">{lbl} ↗</a>'
        )
    if t.get("corroborated"):
        chips.append('<span class="src ok" title="Corroborated: this transaction appears in both a periodic report and the annual report’s Schedule B.">✓</span>')
    return "".join(chips)


def status_chip(status: str) -> str:
    s = status or ""
    cls = "st-mid"
    if s.startswith("Likely Held"):
        cls = "st-hold"
    elif s.startswith("Exercised"):
        cls = "st-done"
    elif s.startswith("Closed"):
        cls = "st-closed"
    elif s.startswith("Reached expiration"):
        cls = "st-unk"
    elif "Partially" in s:
        cls = "st-mid"
    short = s.split(" -> ")[0].split(" (")[0].rstrip(";")
    return f'<span class="chip {cls}" title="{esc(s)}">{esc(short)}</span>'


def conf_chip(c: str) -> str:
    return f'<span class="chip conf-{esc(c)}">{esc(c)}</span>'


# ------------------------------------------------------------------ shell ---
CSS = """
:root{--paper:#F4F6F1;--paper2:#ECEFE6;--ink:#1B241F;--mut:#5A665E;--rule:#C9CFC3;
--ledger:#1E5B44;--ledger-t:#DCE7E0;--oxide:#8C3A2B;--oxide-t:#EFDCD5;--amber:#8F6A12;
--amber-t:#F0E6C9;--low:#6B7570;--low-t:#E4E7E1;--bar:#E0E4D9}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.55 "IBM Plex Sans",system-ui,sans-serif}
a{color:var(--ledger)}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}
.masthead{border-bottom:3px double var(--ink);padding:30px 0 20px}
.eyebrow{font:500 11px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.18em;
color:var(--mut);text-transform:uppercase}
h1{font:400 clamp(30px,4.5vw,44px)/1.1 "Libre Caslon Text",Georgia,serif;margin:.35em 0 .2em}
.dek{max-width:62ch;color:var(--mut);margin:0}
nav{display:flex;gap:26px;margin-top:22px}
nav a{font:600 12px/1 "IBM Plex Mono",monospace;letter-spacing:.14em;color:var(--mut);
text-decoration:none;text-transform:uppercase;padding:8px 0 10px;border-bottom:3px solid transparent}
nav a.on{color:var(--ink);border-color:var(--ledger)}
nav a:hover{color:var(--ink)}
.strip{display:flex;flex-wrap:wrap;gap:8px 30px;padding:12px 0;border-bottom:1px solid var(--rule);
font:13px "IBM Plex Mono",monospace;color:var(--mut)}
.strip b{color:var(--ink);font-weight:600}
h2{font:400 24px "Libre Caslon Text",Georgia,serif;margin:44px 0 6px}
.sub{color:var(--mut);margin:0 0 16px;max-width:72ch}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{font:600 10.5px "IBM Plex Mono",monospace;letter-spacing:.1em;text-transform:uppercase;
text-align:left;padding:8px 10px;border-bottom:2px solid var(--ink);white-space:nowrap}
table.sortable th{cursor:pointer}
table.sortable th:hover{color:var(--ledger)}
td{border-bottom:1px solid var(--rule);padding:10px;vertical-align:top}
tbody tr:hover{background:#fdfdfb}
td.num,td.mono{font-family:"IBM Plex Mono",monospace;font-size:12.5px;white-space:nowrap}
.tick{font:600 13.5px "IBM Plex Mono",monospace}
.aname{display:block;color:var(--mut);font-size:12px;max-width:26ch;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.mut{color:var(--mut)}.nil{color:var(--mut)}
.amt{min-width:170px}
.bar{position:relative;height:7px;background:var(--bar);border-radius:2px;margin:4px 0 5px}
.bar i{position:absolute;top:0;bottom:0;border-radius:2px;background:var(--ink)}
.bar i.buy{background:var(--ledger)}.bar i.sell{background:var(--oxide)}
.bar i.open{background:linear-gradient(90deg,currentColor 55%,transparent);color:var(--ink)}
.bar i.buy.open{color:var(--ledger)}.bar i.sell.open{color:var(--oxide)}
.amt-label{font:12px "IBM Plex Mono",monospace;color:var(--mut);white-space:nowrap}
.bar i.exact{width:3px;background:var(--ink)}
.exact-tag{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink);
border:1px solid var(--rule);border-radius:2px;padding:0 4px;margin-left:3px;cursor:help}
sup.ub,sup.inf{color:var(--amber);font-weight:700;cursor:help}
.chip{display:inline-block;padding:2px 8px;border-radius:3px;
font:500 11px "IBM Plex Mono",monospace;white-space:nowrap}
.act-buy{background:var(--ledger-t);color:var(--ledger)}
.act-sell{background:var(--oxide-t);color:var(--oxide)}
.act-gift{background:var(--oxide-t);color:var(--oxide);outline:1px dashed var(--oxide)}
.act-ex{background:var(--paper2);color:var(--ink);outline:1px solid var(--rule)}
.st-hold{background:var(--ledger-t);color:var(--ledger)}
.st-done{background:var(--ledger);color:#fff}
.st-closed{background:var(--ink);color:var(--paper)}
.st-unk{background:var(--low-t);color:var(--low);outline:1px dashed var(--low)}
.st-mid{background:var(--amber-t);color:var(--amber)}
.conf-high{background:var(--ledger-t);color:var(--ledger)}
.conf-medium{background:var(--amber-t);color:var(--amber)}
.conf-low{background:var(--low-t);color:var(--low);outline:1px dashed var(--low)}
.src{font:600 10.5px "IBM Plex Mono",monospace;border:1px solid var(--rule);border-radius:3px;
padding:2px 6px;margin-right:5px;text-decoration:none;color:var(--ledger);white-space:nowrap}
.src:hover{border-color:var(--ledger)}
.src.ok{color:var(--ledger);border-color:var(--ledger)}
.filter{font:13px "IBM Plex Mono",monospace;padding:7px 10px;border:1px solid var(--rule);
border-radius:3px;background:#fff;width:260px;margin:0 0 12px}
.filter:focus{outline:2px solid var(--ledger)}
.manifesto{border:1px solid var(--ink);padding:22px 26px;margin:40px 0;background:var(--paper2)}
.manifesto h2{margin:0 0 10px}
.manifesto li{margin:7px 0;max-width:78ch}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:34px}
.ledgerlist{list-style:none;padding:0;margin:8px 0;font:13.5px "IBM Plex Mono",monospace}
.ledgerlist li{display:flex;justify-content:space-between;border-bottom:1px dotted var(--rule);padding:6px 0}
details{margin:18px 0}
summary{cursor:pointer;font:600 13px "IBM Plex Mono",monospace;letter-spacing:.06em;color:var(--mut)}
.noterow td{border-bottom:1px solid var(--rule);color:var(--amber);font-size:12px;
padding-top:0;font-family:"IBM Plex Mono",monospace}
.glyph{font-size:17px;background:var(--paper2);border:1px solid var(--rule);padding:14px 18px;
font-family:"IBM Plex Mono",monospace;border-radius:3px}
footer{margin:60px 0 0;border-top:3px double var(--ink);padding:18px 0 40px;
color:var(--mut);font-size:12.5px}
footer .wrap{display:flex;flex-wrap:wrap;gap:6px 34px}
@media (max-width:720px){.wrap{padding:0 16px}.aname{max-width:16ch}}
:focus-visible{outline:2px solid var(--ledger);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""
document.querySelectorAll('table.sortable').forEach(t=>{
 t.querySelectorAll('th').forEach((th,i)=>{
  th.tabIndex=0;
  const sort=()=>{
   const tb=t.tBodies[0],rows=[...tb.rows],asc=th.dataset.asc!=='1';
   t.querySelectorAll('th').forEach(h=>{delete h.dataset.asc;h.removeAttribute('aria-sort');});
   th.dataset.asc=asc?'1':'0';th.setAttribute('aria-sort',asc?'ascending':'descending');
   const val=r=>{const c=r.cells[i],d=c.querySelector('[data-v]')||c;
    const v=(d.dataset&&d.dataset.v!==undefined?d.dataset.v:c.textContent.trim());
    // only pure numbers sort numerically; ISO dates etc. compare as strings
    return /^-?\d+(?:\.\d+)?$/.test(v)?parseFloat(v):v.toLowerCase();};
   rows.sort((a,b)=>{const x=val(a),y=val(b);
    if(typeof x!==typeof y)return (typeof x==='number'?-1:1)*(asc?1:-1);
    return (x<y?-1:x>y?1:0)*(asc?1:-1);});
   rows.forEach(r=>tb.appendChild(r));};
  th.addEventListener('click',sort);
  th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sort();}});
 });
});
document.querySelectorAll('.filter').forEach(inp=>{
 const t=document.getElementById(inp.dataset.table);
 inp.addEventListener('input',()=>{const q=inp.value.toLowerCase();
  [...t.tBodies[0].rows].forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});
});
"""

# ------------------------------------------------------------------- feed ---
def build_feed(txs: list[dict], filings: list[dict], built_iso: str) -> str:
    """Atom feed: one entry per filing (stable IDs = official PDF URLs), whose
    content lists that filing's transactions. Readers and bots get the same
    ranges-only, source-linked data the site shows."""
    site = os.environ.get("SITE_BASE_URL", "").rstrip("/")
    by_filing: dict[str, list[dict]] = {}
    for t in txs:
        by_filing.setdefault(t.get("filing_id") or "?", []).append(t)

    entries = []
    for f in sorted(filings, key=lambda f: f["filed"] or "", reverse=True):
        fid = f["url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".pdf")
        rows = by_filing.get(fid, [])
        lines = []
        for t in sorted(rows, key=lambda t: t["transaction_date"] or ""):
            a = t["amount"]
            amt = (a.get("raw") if a.get("exact") else
                   f"{money(a['min'])}–{money(a['max'])}" if a.get("min") and a.get("max")
                   else f"{money(a['min'])}+" if a.get("min") else "—")
            act, _ = action_of(t)
            lines.append(f"{t['transaction_date']} {t.get('ticker') or t['asset_id']} "
                         f"{act} {amt}")
        updated = f"{f['filed']}T00:00:00Z" if f.get("filed") else built_iso
        entries.append(
            f"<entry><id>{esc(f['url'])}</id>"
            f"<title>{esc(f['label'])} — {len(rows)} transaction(s)</title>"
            f"<link rel=\"alternate\" href=\"{esc(f['url'])}\"/>"
            f"<updated>{updated}</updated>"
            f"<content type=\"text\">{esc(chr(10).join(lines) or 'See filing PDF.')}"
            f"</content></entry>")
    self_link = (f'<link rel="self" href="{esc(site)}/feed.xml"/>' if site else "")
    home_link = (f'<link rel="alternate" href="{esc(site)}/"/>' if site else "")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Nancy Pelosi — Trading Record (official disclosures)</title>"
        "<id>tag:pelosi-tracker,2026:filings</id>"
        f"<updated>{built_iso}</updated>{self_link}{home_link}"
        "<author><name>pelosi-tracker (data: Clerk of the U.S. House)</name></author>"
        + "".join(entries) + "</feed>\n")


NAV = [("index.html", "Overview"), ("transactions.html", "Transactions"),
       ("holdings.html", "Holdings"), ("methodology.html", "Methodology")]


def page(active: str, title: str, body: str, strip: str, built: str) -> str:
    nav = "".join(
        f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in NAV
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Pelosi Trading Record</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=Libre+Caslon+Text&display=swap" rel="stylesheet">
<link rel="alternate" type="application/atom+xml" title="New filings" href="feed.xml">
<style>{CSS}</style></head>
<body>
<header class="masthead"><div class="wrap">
<div class="eyebrow">United States House · Official Financial Disclosures</div>
<h1>Nancy Pelosi — Trading Record</h1>
<p class="dek">Every figure on this site is a disclosed statutory range from an
official filing with the Clerk of the House. Nothing is modeled, averaged, or
inferred — and everything links to its source document.</p>
<nav>{nav}</nav>
</div></header>
<div class="wrap"><div class="strip">{strip}</div></div>
<main class="wrap">{body}</main>
<footer><div class="wrap">
<span>Source: Clerk of the U.S. House of Representatives — official filings only.</span>
<span>Amounts are statutory disclosure ranges, not balances or prices.</span>
<span>Built {esc(built)} · parser {VERSION} · <a href="feed.xml">Atom feed</a></span>
<span>Not investment advice. 5&nbsp;U.S.C.&nbsp;§13107 restricts commercial use of disclosure reports.</span>
</div></footer>
<script>{JS}</script>
</body></html>"""


# ------------------------------------------------------------------ pages ---
def tx_row(t: dict) -> str:
    act, cls = action_of(t)
    tone = "sell" if cls in ("act-sell", "act-gift") else "buy"
    tick = t.get("ticker") or t.get("asset_id")
    return (
        "<tr>"
        f'<td class="mono" data-v="{esc(t["transaction_date"])}">{fmt_date(t["transaction_date"])}</td>'
        f'<td class="mono" data-v="{esc(t.get("filed_date") or "")}">{fmt_date(t.get("filed_date"))}</td>'
        f"{delay_html(t)}"
        f'<td><span class="tick">{esc(tick)}</span>'
        f'<span class="aname" title="{esc(t.get("asset_name"))}">{esc(t.get("asset_name"))}</span></td>'
        f'<td><span class="chip {cls}">{act}</span></td>'
        f"<td>{details_of(t)}</td>"
        f"<td>{amount_html(t.get('amount'), tone)}</td>"
        f"<td>{source_chips(t)}</td>"
        "</tr>"
    )


TX_HEAD = ("<tr><th>Traded</th><th>Filed</th><th>Delay</th><th>Asset</th>"
           "<th>Action</th><th>Details as filed</th><th>Amount (range)</th><th>Source</th></tr>")


def build_transactions_page(txs: list[dict], strip: str, built: str) -> str:
    rows = "".join(tx_row(t) for t in txs)
    body = f"""
<h2>Disclosed transactions</h2>
<p class="sub">All {len(txs)} transactions parsed from the ingested filings,
merged across periodic reports and the annual report’s Schedule&nbsp;B.
<b>Traded</b> is the transaction date; <b>Filed</b> is when the disclosure was
signed; <b>Delay</b> is the gap between them. A&nbsp;<sup class="ub">^</sup>
marks delays measured only to the annual report — an upper bound.
Click a column to sort; every row links to the official PDF.</p>
<input class="filter" data-table="tx" placeholder="Filter — ticker, action, year…" aria-label="Filter transactions">
<table class="sortable" id="tx"><thead>{TX_HEAD}</thead><tbody>{rows}</tbody></table>
"""
    return page("transactions.html", "Transactions", body, strip, built)


def movements_html(ms: list[dict]) -> str:
    if not ms:
        return '<span class="mut">—</span>'
    out = []
    for m in ms[:6]:
        n = m.get("shares") if m.get("shares") is not None else m.get("units")
        if n is None:
            continue
        sign = "+" if n > 0 else "−"
        unit = "sh" if m.get("unit", "shares") == "shares" else m.get("unit", "")
        gift = " (gift)" if m.get("is_gift") else ""
        out.append(f'{fmt_date(m["date"])} {sign}{abs(n):,} {unit}{gift}')
    more = f' <span class="mut">+{len(ms) - 6} more</span>' if len(ms) > 6 else ""
    return '<span class="mono" style="font-size:12px">' + "<br>".join(out) + more + "</span>"


def build_holdings_page(h: dict, strip: str, built: str) -> str:
    stocks = "".join(
        "<tr>"
        f'<td><span class="tick">{esc(s.get("ticker") or s["asset_id"])}</span>'
        f'<span class="aname" title="{esc(s["name"])}">{esc(s["name"])}</span></td>'
        f"<td>{amount_html(s.get('period_end_value'), 'hold')}</td>"
        f'<td class="mono">{esc(s.get("income_types") or "—")}</td>'
        f"<td>{status_chip(s['status'])}<br>{conf_chip(s['confidence'])}"
        + (f'<div class="mut" style="font-size:11.5px;max-width:24ch">{esc("; ".join(s.get("notes", [])))}</div>' if s.get("notes") else "")
        + "</td>"
        f"<td>{movements_html(s.get('disclosed_movements', []))}</td>"
        "</tr>"
        for s in h["stocks"]
    )
    lots = "".join(
        "<tr>"
        f'<td><span class="tick">{esc(o["asset_id"])}</span>'
        f'<span class="aname">{esc(o["name"])}</span></td>'
        f'<td class="mono">{o["contracts"]:,} {esc(o["option_type"])}s @ {money(int(o["strike"]))} '
        f'→ exp {fmt_date(o.get("expiration"))}</td>'
        f'<td class="mono">{fmt_date(o.get("acquired"))}</td>'
        f"<td>{status_chip(o['status'])}</td>"
        f"<td>{conf_chip(o['confidence'])}</td>"
        "</tr>"
        for o in h["options"]
    )
    others = "".join(
        "<tr>"
        f'<td>{esc(o["name"])}'
        + (f'<span class="aname">{esc(o.get("location") or "")}</span>' if o.get("location") else "")
        + "</td>"
        f'<td class="mono">{esc(o.get("instrument") or "")}</td>'
        f'<td class="mono">{esc(o.get("owner") or "—")}</td>'
        f"<td>{amount_html(o.get('value_range'), 'hold')}</td>"
        f'<td class="mono">{esc(o.get("income_types") or "—")}</td>'
        "</tr>"
        + (f'<tr class="noterow"><td colspan="5">{esc("; ".join(o["notes"]))}</td></tr>'
           if o.get("notes") else "")
        for o in h["other_assets"]
    )
    body = f"""
<h2>Estimated holdings</h2>
<p class="sub"><b>These are estimates, and they are labeled as such.</b>
Congress discloses value <i>ranges</i> at a reporting date plus subsequent
transactions — never share balances. Baseline: annual report as of
<b>{fmt_date(h["baseline_period_end"])}</b>, updated with every disclosed
transaction since, evaluated as of <b>{fmt_date(h["as_of"])}</b>.
{esc(h.get("method", ""))}.</p>

<h2 style="font-size:20px">Public securities</h2>
<input class="filter" data-table="stk" placeholder="Filter tickers…" aria-label="Filter holdings">
<table class="sortable" id="stk"><thead><tr><th>Asset</th>
<th>Reported value ({fmt_date(h["baseline_period_end"])})</th><th>{esc(h["baseline_period_end"][:4])} income</th>
<th>Status</th><th>Disclosed movements</th></tr></thead>
<tbody>{stocks}</tbody></table>

<h2 style="font-size:20px">Option lots</h2>
<p class="sub">Each lot is tracked (underlying, type, strike, expiry) through
its lifecycle. Lots past expiration with no disclosed outcome are marked
exactly that — the filings haven’t said, so neither do we.</p>
<table class="sortable"><thead><tr><th>Underlying</th><th>Lot</th>
<th>Acquired</th><th>Status</th><th>Confidence</th></tr></thead>
<tbody>{lots}</tbody></table>

<details><summary>Other reported assets — LLCs, real estate, accounts ({len(h["other_assets"])})</summary>
<table><thead><tr><th>Asset</th><th>Type</th><th>Owner</th><th>Reported value</th><th>Income type</th></tr></thead>
<tbody>{others}</tbody></table></details>
"""
    return page("holdings.html", "Holdings", body, strip, built)


def build_index(txs, h, filings, strip, built) -> str:
    latest = sorted(txs, key=lambda t: t["transaction_date"], reverse=True)[:8]
    rows = "".join(tx_row(t) for t in latest)

    def count(pred, seq):
        return sum(1 for x in seq if pred(x))

    snap = f"""
<ul class="ledgerlist">
<li><span>Stocks — likely held</span><b>{count(lambda s: s["status"].startswith("Likely"), h["stocks"])}</b></li>
<li><span>Stocks — closed at period end</span><b>{count(lambda s: s["status"].startswith("Closed"), h["stocks"])}</b></li>
<li><span>Option lots — likely held</span><b>{count(lambda o: o["status"].startswith("Likely"), h["options"])}</b></li>
<li><span>Option lots — exercised</span><b>{count(lambda o: o["status"].startswith("Exercised"), h["options"])}</b></li>
<li><span>Option lots — expired, outcome undisclosed</span><b>{count(lambda o: o["status"].startswith("Reached"), h["options"])}</b></li>
<li><span>Other assets (LLCs, property, accounts)</span><b>{len(h["other_assets"])}</b></li>
</ul>"""

    fl = "".join(
        f'<li><span><a href="{esc(f["url"])}" target="_blank" rel="noopener">{esc(f["label"])}</a></span>'
        f'<b>{fmt_date(f["filed"])}</b></li>'
        for f in filings
    )
    body = f"""
<div class="cols" style="margin-top:34px">
<div>
<h2 style="margin-top:0">Latest disclosed activity</h2>
<p class="sub">Most recent transactions across all ingested filings.
<a href="transactions.html">Full record →</a></p>
<table>{'<thead>' + TX_HEAD + '</thead>'}<tbody>{rows}</tbody></table>
</div>
</div>
<div class="cols">
<div>
<h2>Holdings snapshot</h2>
<p class="sub">As of {fmt_date(h["as_of"])} · <a href="holdings.html">full estimates →</a></p>
{snap}
</div>
<div>
<h2>Filings ingested</h2>
<p class="sub">Official PDFs at the Clerk of the House.</p>
<ul class="ledgerlist">{fl}</ul>
<p class="sub" style="font-size:12.5px;margin-top:10px">
<b>Known gap:</b> a report signed June&nbsp;23,&nbsp;2026 is missing from this
record — its official PDF wasn’t retrievable, and this tracker won’t transcribe
filings from news coverage. <a href="methodology.html">Why →</a></p>
</div>
</div>
<div class="manifesto">
<h2>What this tracker will not show you</h2>
<ul>
<li><b>Modeled profit &amp; loss.</b> Trade sizes are disclosed as ranges; a “+$310K gain” computed from range midpoints is fiction wearing a decimal point.</li>
<li><b>Invented share balances.</b> We show disclosed movements (exact counts when filed) — never a running total the filings don’t support.</li>
<li><b>Aggregator data.</b> Every row parses from an official Clerk PDF, linked beside it. Third-party trackers are used only to cross-check our parser.</li>
<li><b>Unlabeled guesses.</b> Estimates carry a status, a confidence grade, and the evidence trail that produced them.</li>
</ul>
</div>
"""
    return page("index.html", "Overview", body, strip, built)


def build_methodology(strip, built) -> str:
    buckets = "".join(
        f'<tr><td class="mono">{money(lo)}{"–" + money(hi) if hi else "+"}</td></tr>'
        for lo, hi in KNOWN_BUCKETS
    )
    enc = "".join(chr(0x283 + ord(c) - 97) if "a" <= c <= "z" else c for c in "description")
    body = f"""
<h2>Methodology</h2>
<p class="sub">The pipeline and its rules, in the open.</p>

<h2 style="font-size:20px">Sources</h2>
<p>Only official filings from the Clerk of the U.S. House of Representatives:
Periodic Transaction Reports (PTRs, required within 30–45 days of a trade
under the STOCK Act) and Annual Financial Disclosure reports (Schedule&nbsp;A
holdings, Schedule&nbsp;B transactions). The poller diffs the Clerk’s yearly
index and fetches new documents directly; every parsed row stores its filing
ID and source URL.</p>

<h2 style="font-size:20px">Confirmed vs. estimated</h2>
<p><b>Transactions are parsed facts</b> — dates, statutory amount ranges, and
the filed description, including option lots (contracts / strike / expiry),
exercises, and charitable gifts, which the forms encode only in prose.
<b>Holdings are estimates</b>: an annual-report baseline advanced by each
disclosed transaction. Statuses: <i>Likely Held · Partially Sold · Closed ·
Exercised · Reached expiration, outcome undisclosed</i>. Confidence:
<span class="chip conf-high">high</span> <span class="chip conf-medium">medium</span>
<span class="chip conf-low">low</span> — degraded automatically as evidence
ages. Share balances are never computed; only disclosed movements are shown.</p>

<h2 style="font-size:20px">Two dates, one delay</h2>
<p>Every transaction carries its trade date and its filing date; the gap is
the disclosure delay. When a transaction is known only from an annual report,
the delay is flagged <sup class="ub">^</sup> as an upper bound rather than
passed off as the real lag.</p>

<h2 style="font-size:20px">Amounts are buckets</h2>
<div class="cols"><div>
<p>Congress discloses dollar figures only as these statutory ranges — so
that’s what we render, as bars on a log scale. If a page break truncates a
filed range, the upper bound is restored from this table and marked
<sup class="inf">*</sup>.</p></div>
<div><table style="max-width:280px"><thead><tr><th>Statutory ranges</th></tr></thead>
<tbody>{buckets}</tbody></table></div></div>

<h2 style="font-size:20px">Duplicates &amp; amendments</h2>
<p>The same trade often appears in a PTR and again in the annual report’s
Schedule&nbsp;B. Rows are merged on (date, asset, type, amount, owner); the
PTR is the base record, every source is kept in the provenance trail, and
corroborated rows are marked ✓. Amended filings supersede originals without
erasing them.</p>

<h2 style="font-size:20px">A small decoding story</h2>
<p>The Clerk’s e-filing template renders its labels in a custom font that maps
lowercase letters to Unicode IPA glyphs (U+0283–U+029C). Extracted text
looks like this:</p>
<p class="glyph">{esc(enc)}: &nbsp;→&nbsp; description:</p>
<p>We translate the cipher instead of guessing around it, so parsing is
identical across PDF extractors — and asset names never bleed into
description text.</p>

<h2 style="font-size:20px">Known gaps</h2>
<p>One periodic report signed June&nbsp;23,&nbsp;2026 is <b>not ingested</b>:
its official PDF could not be retrieved, and this pipeline does not accept
secondary reporting as a source, so trades disclosed only there are missing
from this record rather than transcribed from news coverage. Paper-filed
(scanned) reports route to an OCR path that is stubbed, not built. When
something isn’t known, the site says so instead of smoothing over it.</p>

<h2 style="font-size:20px">Legal</h2>
<p>Financial disclosure reports may not be used for commercial purposes
(5&nbsp;U.S.C.&nbsp;§13107, with a news-media exception). This tracker is a
free public-interest resource and not investment advice.</p>
"""
    return page("methodology.html", "Methodology", body, strip, built)


# ------------------------------------------------------------------- main ---
def load_filings() -> list[dict]:
    db = OUT / "pelosi.db"
    out = []
    if db.exists():
        con = sqlite3.connect(db)
        for fid, doc, ftype, _n, _sd, year, _fd, signed, url, *_ in con.execute(
            "select * from filings order by signed_date"
        ):
            label = f"Annual Report {year} · #{fid}" if doc == "fd" else f"Periodic Transaction Report · #{fid}"
            out.append({"label": label, "filed": signed, "url": url})
        con.close()
    return out


def build(site_dir: Path = SITE) -> dict:
    txs = json.loads((OUT / "transactions.json").read_text())
    h = json.loads((OUT / "holdings.json").read_text())
    filings = load_filings()
    txs_sorted = sorted(txs, key=lambda t: t["transaction_date"], reverse=True)

    ptr_delays = [t["disclosure_delay_days"] for t in txs
                  if t.get("delay_basis") == "ptr" and t.get("disclosure_delay_days") is not None]
    med = int(statistics.median(ptr_delays)) if ptr_delays else None
    dates = [t["transaction_date"] for t in txs]
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    strip = (
        f"<span><b>{len(filings)}</b> filings</span>"
        f"<span><b>{len(txs)}</b> transactions</span>"
        f"<span>trades <b>{fmt_date(min(dates))}</b> – <b>{fmt_date(max(dates))}</b></span>"
        f"<span>last filing <b>{fmt_date(max(f['filed'] for f in filings))}</b></span>"
        + (f"<span>median PTR delay <b>{med}d</b></span>" if med is not None else "")
        + (f'<span>coverage through <b>{fmt_date(h["coverage"]["filed_through"])}</b>'
           + (f' · <b>{len(h["coverage"]["known_gaps"])}</b> known gap</span>'
              if h["coverage"]["known_gaps"] else "</span>")
           if h.get("coverage", {}).get("filed_through") else "")
    )

    site_dir.mkdir(exist_ok=True)
    pages = {
        "index.html": build_index(txs_sorted, h, filings, strip, built),
        "transactions.html": build_transactions_page(txs_sorted, strip, built),
        "holdings.html": build_holdings_page(h, strip, built),
        "methodology.html": build_methodology(strip, built),
    }
    for name, htm in pages.items():
        (site_dir / name).write_text(htm)
    built_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (site_dir / "feed.xml").write_text(build_feed(txs_sorted, filings, built_iso))
    return {"pages": list(pages) + ["feed.xml"], "transactions": len(txs),
            "dir": str(site_dir)}


if __name__ == "__main__":
    info = build()
    print(f"built {len(info['pages'])} pages -> {info['dir']} ({info['transactions']} transactions)")
