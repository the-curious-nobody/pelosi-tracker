-- Pelosi Trading Tracker - storage schema (SQLite)
-- Every derived record traces back to an official filing (filing_id -> PDF).

CREATE TABLE IF NOT EXISTS filings (
    filing_id      TEXT PRIMARY KEY,          -- Clerk DocID, e.g. 20026590
    doc_type       TEXT NOT NULL,             -- ptr | fd
    filing_type    TEXT,                      -- Annual Report / Amendment / ...
    filer_name     TEXT,
    state_district TEXT,
    filing_year    TEXT,
    filing_date    TEXT,                      -- as printed on the filing
    signed_date    TEXT,                      -- ISO
    source_url     TEXT,                      -- official PDF URL
    fetched_via    TEXT,                      -- official | mirror | fixture
    parser_version TEXT,
    ingested_at    TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id       TEXT PRIMARY KEY,          -- ticker or name slug
    ticker         TEXT,
    canonical_name TEXT,
    is_security    INTEGER,
    instruments    TEXT,                      -- JSON list
    name_variants  TEXT                       -- JSON list
);

CREATE TABLE IF NOT EXISTS transactions (
    id                TEXT PRIMARY KEY,       -- filing_id:row_order (pre-merge)
    asset_id          TEXT REFERENCES assets(asset_id),
    ticker            TEXT,
    asset_name        TEXT,
    asset_type_code   TEXT,
    instrument        TEXT,
    owner_code        TEXT,                   -- SP/JT/DC/NULL(filer)
    transaction_code  TEXT,                   -- P / S / S (partial) / E
    transaction_date  TEXT,                   -- ISO
    notification_date TEXT,                   -- ISO (PTR only)
    amount_min        INTEGER,
    amount_max        INTEGER,
    amount_raw        TEXT,
    amount_max_inferred INTEGER DEFAULT 0,
    filing_status     TEXT,                   -- New / Amended (PTR)
    description       TEXT,
    comments          TEXT,
    facts             TEXT,                   -- JSON: lots/exercise/shares/gift
    disclosure_delay_days INTEGER,            -- filed vs traded
    corroborated      INTEGER DEFAULT 0,
    provenance        TEXT,                   -- JSON list of {filing_id, schedule}
    needs_review      INTEGER DEFAULT 0,
    notes             TEXT                    -- JSON list
);

CREATE TABLE IF NOT EXISTS positions (        -- annual Schedule A rows
    id              TEXT PRIMARY KEY,         -- filing_id:row_order
    filing_id       TEXT REFERENCES filings(filing_id),
    asset_id        TEXT REFERENCES assets(asset_id),
    ticker          TEXT,
    asset_name      TEXT,
    asset_type_code TEXT,
    instrument      TEXT,
    owner_code      TEXT,
    value_min       INTEGER,
    value_max       INTEGER,
    value_raw       TEXT,
    income_types    TEXT,
    income_raw      TEXT,
    description     TEXT,
    location        TEXT,
    facts           TEXT,
    needs_review    INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS holdings (         -- recomputed estimates, never facts
    asset_id    TEXT,
    kind        TEXT,                         -- stock | option | other
    payload     TEXT,                         -- full JSON estimate incl. evidence
    as_of       TEXT,
    PRIMARY KEY (asset_id, kind, payload)
);

CREATE INDEX IF NOT EXISTS idx_tx_date   ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_tx_asset  ON transactions(asset_id);
CREATE INDEX IF NOT EXISTS idx_pos_asset ON positions(asset_id);
