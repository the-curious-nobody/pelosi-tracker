# Pelosi Trading Tracker — ingestion & parsing core

Accuracy-first pipeline that turns official U.S. House financial disclosures
(Periodic Transaction Reports + Annual Financial Disclosure reports) into
structured, provenance-tracked transaction and holdings data for
Rep. Nancy Pelosi — architected to expand to any House member.

**Status: pipeline and site validated against real filings.** Five official
filings (four PTRs + the 2025 Annual Report, through the one signed
2026-08-21) are committed as text fixtures and covered by a regression suite:
16/16 tests pass, 54 raw rows → 37 merged transactions, 68 asset positions,
0 rows needing review.

## Design principles (from the product spec)

| Principle | Where it's enforced |
|---|---|
| Official sources only | `ingest.py` polls `disclosures-clerk.house.gov` yearly ZIP indexes; fixture provenance recorded per filing |
| Confirmed vs estimated | `transactions` are parsed facts; `holdings.py` outputs *estimates* with status + confidence + evidence trail |
| No fake precision | Amounts stay statutory ranges (`amounts.py`); repaired bounds carry `max_inferred`, precise filed figures carry `exact` — never silent |
| Never compute share balances | `holdings.py` emits `disclosed_movements` (share counts as filed) — no invented running balances |
| Dual dates + delay | transaction date vs notification vs filing date; FD-only rows get `delay_basis="annual_report_upper_bound"` (`^` in reports) |
| Amendment/dup handling | cross-source dedupe keyed on (date, asset, code, amount.min, owner); PTR wins as base record, all sources kept in `provenance` |

## Repo layout

```
pelosi_tracker/
  textnorm.py      glyph-cipher decode (U+0283..U+029C -> a-z), furniture/label/meta detection
  amounts.py       statutory buckets; dangling-range repair with max_inferred flag
  descriptions.py  option lots, exercises, share counts, gifts from D: prose
  filings.py       line state machine: PTR rows, FD Schedule A positions, FD Schedule B transactions
  assets.py        canonical asset registry (ticker or name-slug), type-code map
  holdings.py      dedupe + holdings reconstruction (statuses, confidence, staleness degradation)
  ingest.py        production poller: ZIP index diff -> PDF download -> text extraction
  db.py, schema.sql  SQLite persistence with provenance JSON
fixtures/          verbatim text extractions of real official filings
tests/             regression suite pinned to the real fixtures
run_demo.py        end-to-end: fixtures -> out/{transactions,positions,assets,holdings}.json + pelosi.db
```

## Run it

```bash
python3 run_demo.py            # parse fixtures, write out/, print report
python3 build_site.py          # render static site -> site/ (4 self-contained pages)
python3 -m tests.test_parsers  # regression suite (stdlib only, no deps)
```

Core parsing is **stdlib-only**. Live polling needs network access to
`disclosures-clerk.house.gov` plus (optionally) `pdfplumber` if `pdftotext`
isn't installed — see `requirements.txt`.

```bash
# live sync (outside sandbox, or after allowlisting the Clerk's domain):
python3 -m pelosi_tracker.ingest --data data/ --years 2025 2026 --last Pelosi
```

## Parsing details that matter

- **Glyph cipher.** The Clerk's e-filing template renders labels in a custom
  font mapping lowercase a–z to Unicode U+0283–U+029C ("Dʇʕʅʔʋʒʖʋʑʐ" =
  "Description"). We decode deterministically, so labels parse identically
  across pdftotext / pdfplumber / other extractors.
- **Page-break repair.** Mid-row page breaks (repeated column headers,
  `Filing ID #` footers) are stripped as furniture; orphaned description lines
  re-attach to the last completed row; truncated `$X -` ranges are repaired
  from the statutory bucket table and flagged.
- **Semantics live in prose.** `S (partial)` may be a charitable gift; `P`
  may be an option exercise. `descriptions.py` preserves this instead of
  flattening to buy/sell.
- **Options as lots.** Option positions are keyed (asset, type, strike,
  expiry) through their lifecycle: purchase → exercised / sold / reached
  expiration with unknown outcome (never guessed).

## Validated vs pending

Validated: full pipeline on real filings 20026590, 20033337, 10075701,
20033725, 20035143. Cross-corroboration PTR↔FD works (17 transactions carry
two sources), and ingesting the January 2026 PTR both resolved all five
1/16/26-expiry option lots as exercises and replaced the annual-report delay
upper bounds for the December 2025 trades with real PTR delays.

Known gap: the PTR signed **2026-06-23** (May 29 INTC/UBER call purchases) is
*not* ingested. Its official PDF was not retrievable in this environment, and
the pipeline does not accept secondary reporting as a source — so those trades
are absent and disclosed as absent, rather than transcribed from news coverage.
Both the overview and methodology pages state this.

Pending: live index polling (sandbox blocks the Clerk's domain — allowlist
`disclosures-clerk.house.gov` to run `ingest.sync` end-to-end, which would
close the gap above automatically); paper-filed (scanned) reports need an OCR
path (`is_electronic` heuristic already routes them).

## Going live

"Live" for this data means a scheduled poller, not a server: filings land a
few times a month with a 30–45-day statutory lag, and the Clerk republishes
the yearly index daily. The right architecture is static hosting plus a cron
rebuild — free, fast, and with no runtime to attack or maintain.

Everything needed ships in this repo:

1. Push to GitHub. In the repo settings enable **Pages → Source: GitHub
   Actions**, and under Actions grant the workflow read/write contents.
2. `.github/workflows/update.yml` then runs every 3 hours (and on demand):
   it syncs the Clerk's index for the current and previous year, downloads
   and extracts any new filing, rebuilds the dataset with `--fail-on-review`
   (a filing the parser can't fully handle publishes *nothing* and emails
   you instead), runs all three test suites, rebuilds the site + Atom feed,
   commits `data/` and the JSON outputs back to the repo — git is the audit
   trail; every data change is a reviewable diff — and deploys to Pages.
3. Optional: set a repo variable `SITE_BASE_URL` for absolute feed links,
   and a custom domain on Pages.

The first live sync backfills every indexed Pelosi filing for the requested
years — including the June 23, 2026 PTR this dev environment couldn't reach —
and `known_gaps` becomes fully computed: any filing the index lists that the
pipeline couldn't parse (paper filings pending OCR, extraction failures) is
declared on the site rather than silently dropped. Widen `--years` in the
workflow to backfill history; pre-e-filing paper reports will accumulate as
declared gaps until the OCR path is built.

Users subscribe to `feed.xml` (Atom, one entry per filing, linking the
official PDF). Polling stays polite: a few index checks per day with an
identifying User-Agent, which is both courteous and all the freshness the
disclosure regime permits.

## Legal note

5 U.S.C. § 13107 restricts commercial use of these reports (news-media
dissemination to the public excepted). A free public tracker is the safe
default; consult counsel before monetizing.
