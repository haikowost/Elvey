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
