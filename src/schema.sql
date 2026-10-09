-- Elvey KYC / lightweight CRM schema. Safe to run repeatedly.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id                   INTEGER PRIMARY KEY,
    source_id            TEXT,               -- consolidation account_id (A0001)
    name                 TEXT NOT NULL,
    name_norm            TEXT NOT NULL,
    accno                TEXT,
    segment              TEXT NOT NULL DEFAULT 'customer',  -- competitor|internal|customer
    division             TEXT,
    cluster              TEXT,
    branch               TEXT,
    rep                  TEXT,
    latest_sellout       REAL,
    brand_focus          TEXT,
    allocation           TEXT,
    kyc_status           TEXT,
    linkedin_company_url TEXT,
    rank                 INTEGER,
    verified             INTEGER NOT NULL DEFAULT 0,
    source               TEXT,
    extra                TEXT,               -- JSON: category, axis partner, Tasha visit, ...
    zoho_account_id      TEXT,
    zoho_last_synced     TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_accounts_norm ON accounts(name_norm);
CREATE INDEX IF NOT EXISTS ix_accounts_accno ON accounts(accno);
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_source ON accounts(source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS contacts (
    id                   INTEGER PRIMARY KEY,
    source_id            TEXT,               -- consolidation contact_id (C00001)
    account_id           INTEGER REFERENCES accounts(id),
    full_name            TEXT NOT NULL,
    name_norm            TEXT NOT NULL,
    first_name           TEXT,
    last_name            TEXT,
    role                 TEXT,
    role_source          TEXT,               -- data|pending|linkedin|manual (pending/None may be filled by the harvester)
    allocated_rep        TEXT,               -- the AM this contact is allocated to ('Unallocated' -> allocated=0)
    allocated            INTEGER NOT NULL DEFAULT 1,
    category             TEXT,               -- call cadence: A weekly, B monthly, C ad-hoc, D+ to triage
    in_zoho              TEXT,               -- Y|N|Company only, from the source sheet; kept live thereafter
    in_makdb             TEXT,               -- Y|N, from the source sheet
    segment_override     TEXT,               -- pins one role when it differs from the account's (v4 §1)
    contact_class        TEXT,               -- engaged|lead|backlog; NULL = unclassified (v4 §2)
    zoho_last_activity   TEXT,               -- 'Last Activity Time' from a Zoho Contacts export (v4 §2)
    qreg_last_quote      TEXT,               -- latest Quote Date from a rep's QReg sheet (v4 §2)
    reports_to_id        INTEGER REFERENCES contacts(id),  -- contact-level org chart, any account (v4 §3)
    top500_rank          INTEGER,            -- view_top500_contacts rank (relational workbook), drives harvest order
    email                TEXT,
    tel                  TEXT,
    cell                 TEXT,
    linkedin_contact_url TEXT,
    segment              TEXT NOT NULL DEFAULT 'customer',
    org_group            TEXT,               -- People Tree sub-group override
    priority             INTEGER,
    extra                TEXT,               -- JSON
    image_filename       TEXT,
    image_status         TEXT NOT NULL DEFAULT 'none',     -- none|downloaded|no_photo|manual
    linkedin_summary     TEXT,
    linkedin_experience  TEXT,               -- JSON list of {title, company, dates}
    linkedin_profile_url TEXT,               -- profile actually matched by the harvester
    department           TEXT,               -- Management|Sales|Technical|Projects|Procurement|Finance|...
    department_source    TEXT,               -- manual|role|linkedin|email
    city                 TEXT,
    province             TEXT,               -- Zoho calls this field 'State'
    country              TEXT,
    location_source      TEXT,               -- linkedin (only source captured so far)
    employment_status    TEXT NOT NULL DEFAULT 'unknown',  -- unknown|current|moved (per LinkedIn)
    linkedin_current_company TEXT,           -- current employer according to LinkedIn
    moved_to_account_id  INTEGER REFERENCES accounts(id), -- suggested account when they moved
    contact_status       TEXT NOT NULL DEFAULT 'active',   -- active|left|not_relevant
    status_note          TEXT,
    enrich_status        TEXT NOT NULL DEFAULT 'pending',  -- pending|done|no_profile|failed
    enrich_attempts      INTEGER NOT NULL DEFAULT 0,
    enrich_last_at       TEXT,
    enrich_error         TEXT,
    zoho_contact_id      TEXT,
    zoho_photo_hash      TEXT,               -- sha1 of the last photo uploaded to Zoho
    zoho_last_synced     TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_contacts_email ON contacts(lower(email));
CREATE INDEX IF NOT EXISTS ix_contacts_acct_name ON contacts(account_id, name_norm);
CREATE INDEX IF NOT EXISTS ix_contacts_priority ON contacts(priority);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_source ON contacts(source_id) WHERE source_id IS NOT NULL;

-- One row per LinkedIn visit; drives the daily cap and gives an audit trail.
CREATE TABLE IF NOT EXISTS harvest_log (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER REFERENCES contacts(id),
    day         TEXT NOT NULL,              -- YYYY-MM-DD local
    result      TEXT NOT NULL,              -- done|no_profile|no_photo|failed|stopped
    detail      TEXT,
    ts          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_harvest_day ON harvest_log(day);

-- Last pull of Zoho records (read-only mirror used for diffing).
CREATE TABLE IF NOT EXISTS zoho_records (
    module     TEXT NOT NULL,               -- Accounts|Contacts
    zoho_id    TEXT NOT NULL,
    data       TEXT NOT NULL,               -- JSON record as returned by Zoho
    pulled_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (module, zoho_id)
);

CREATE TABLE IF NOT EXISTS zoho_sync_log (
    id             INTEGER PRIMARY KEY,
    entity         TEXT NOT NULL,           -- account|contact
    local_id       INTEGER NOT NULL,
    zoho_id        TEXT,
    action         TEXT NOT NULL,           -- create|update|link|skip|photo
    fields_pushed  TEXT,                    -- JSON
    result         TEXT NOT NULL,           -- ok|error:<msg>
    ts             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- An account can be more than one thing (e.g. a sub-distributor is a customer AND a supplier).
-- accounts.segment stays the primary role; this is the authoritative full set (v4 spec §1).
CREATE TABLE IF NOT EXISTS account_roles (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    role       TEXT NOT NULL,               -- customer|supplier|competitor|internal
    source     TEXT NOT NULL,               -- primary|zoho|sheet|linkedin|manual
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, role)
);

-- Deal Intelligence Graph (/graph). All optional: the graph degrades to accounts + people when
-- these are empty. Loaded with `python -m src.graph import <file.json>`; the UI never writes.
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    sub        TEXT,                        -- one-line description
    region     TEXT,
    brands     TEXT,                        -- comma-separated brand / product lines
    facts      TEXT,                        -- JSON list of strings
    source_key TEXT UNIQUE                  -- the import file's own id, so re-imports update in place
);

CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    sub        TEXT,
    brand      TEXT,
    source_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS opportunities (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    stage       TEXT,                       -- free text: Discovery|Qualified|Quoted|Tender|...
    value       TEXT,                       -- display value as captured, e.g. 'R 1.2m'
    account_id  INTEGER REFERENCES accounts(id),
    project_id  INTEGER REFERENCES projects(id),
    source_key  TEXT UNIQUE
);

-- Typed edges between graph node ids ('a12' account, 'c5' contact, 'p3' project, 'pr2' product).
CREATE TABLE IF NOT EXISTS graph_links (
    id      INTEGER PRIMARY KEY,
    source  TEXT NOT NULL,
    target  TEXT NOT NULL,
    rel     TEXT NOT NULL,                  -- works_at|part_of|does_business_with|involved_in|specifies|serves|competes|opportunity|knows
    ex      TEXT,                           -- JSON extras, e.g. {"owner": "Quin"}
    UNIQUE (source, target, rel)
);

-- Relational-workbook facts (Elvey Consolidated Relational Database: fact_sellout / fact_quotes).
-- Loaded by src/ingest.py on every run, keyed so re-ingest updates in place (idempotent).
CREATE TABLE IF NOT EXISTS sellout (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    brand      TEXT NOT NULL,               -- dim_brands.brand, e.g. 'IQSIGHT / Bosch'
    portfolio  INTEGER NOT NULL DEFAULT 0,  -- 1 = one of Haiko's portfolio brands (dim_brands.portfolio_flag)
    fy25       REAL,
    fy26       REAL,
    PRIMARY KEY (account_id, brand)
);

CREATE TABLE IF NOT EXISTS quotes (
    id          INTEGER PRIMARY KEY,
    source_key  TEXT UNIQUE,                -- 'Q:<quote_id>' from fact_quotes
    q_no        TEXT,
    rep         TEXT,
    quote_date  TEXT,                       -- YYYY-MM-DD
    for_contact TEXT,
    ref         TEXT,
    account_id  INTEGER REFERENCES accounts(id),
    value       REAL,
    gp          REAL
);
CREATE INDEX IF NOT EXISTS ix_quotes_account ON quotes(account_id);
