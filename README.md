# Elvey KYC — lightweight CRM + Zoho sync

A local, resumable KYC tool for Elvey Group's Projects/Pentagon division. It:

1. **Ingests** the Customer Data Consolidation output: the top 50 accounts, the top 500 contacts, and the competitor and internal branches.
2. **Matches** the KYC face library (`<Segment>__<First>_<Last>.jpg`) to contacts.
3. **Enriches** contacts from LinkedIn: a face, a short summary and the last 3–5 roles. It drives your own logged-in browser, with a daily cap and a checkpoint after every contact.
4. Serves a **local dashboard**: the People Tree (Competitors / Internal / Customers) and a Zoho **review-and-select** screen.
5. **Syncs to Zoho CRM**. It first pulls Zoho and diffs it against the local DB (new / changed / in-sync). You pick the records and fields to send. It pushes only those, uploads photos, writes the Zoho IDs back and logs every write.

Everything lives in one SQLite file (`elvey_kyc.db`). Every command can be re-run safely.

### v3 (2026-09-30): the cleaned Consolidated Sales Contacts sheet

The authoritative source is now the **cleaned, hand-synced Consolidated Sales Contacts sheet**
(`Elvey Consolidated Sales Contacts - CLEANED <date>.xlsx`, tab `Customers (import)`) — set its
path in `config.yaml`'s `inputs.workbook`. It's a flat, one-row-per-contact sheet carrying the
rep allocation directly (`AM`), a call-cadence `Category` (A weekly · B monthly · C ad-hoc ·
D+ to triage), and `In Zoho` / `In MakDB` seed flags. What changed:

- **Role now actually reaches the sheet from LinkedIn.** The sheet's placeholder for a
  not-yet-looked-up role is the text `lookup pending (KYC app)` — a real (non-blank) string, so
  the harvester used to treat it as "already has a role" and skip it. Roles are now tracked with
  a `role_source` (`data` | `pending` | `linkedin` | `manual`): the harvester fills a role whose
  source is blank/`pending`/a stale `linkedin` guess, and **never** overwrites a real spreadsheet
  role (`data`) or a human edit (`manual`). This is the one specific gap this round closes.
- **Allocation.** Every contact carries `allocated_rep` (the `AM` column) and `allocated`
  (0 when `AM` is `Unallocated`, 1 otherwise). Unallocated rows are still ingested — nothing is
  dropped — just flagged, so the dashboard can default to allocated-first and filter them out.
- **Phones** are standardised to `+27 ## ### ####` on ingest (unchanged from v2, confirmed still
  matches this sheet's existing format).
- **Filters.** The People Tree has `Allocated rep` and `Category` filters alongside the existing
  department/status ones, and every contact list sorts allocated contacts first within its group.
- **Priority fallback.** When a contact has no explicit `Sort`/`Rank` value, its priority used to
  default to its row position in the sheet — so whoever happened to be listed first always showed
  up first everywhere (People Tree, Corrections chat), regardless of how urgent they actually
  were. It now falls back to the `Category` cadence instead (A before B before C before D+), with
  name as the tiebreaker within a category — reported, root-caused and fixed.
- `In Zoho` / `In MakDB` are carried through as-is from the sheet (`contacts.in_zoho` /
  `contacts.in_makdb`); whether a contact is *actually* linked in Zoho right now is still the
  separate, live `zoho_contact_id` (shown as the "in Zoho" tag) that `src/zoho.py` maintains.

### v4 phase 1 (2026-10-01): account roles, contact classification, org-chart seed

Schema + data foundation for the rebuild in `V4-SPEC.md` — no UI yet, just the DB/API groundwork.

- **`account_roles`** (`src/orgchart.py`): an account can be more than one thing (a sub-distributor
  is a customer *and* a supplier). `sync_account_roles()` runs on every ingest — it tags every
  account with its primary `segment` role, then infers `supplier` from the sheet's
  `Installer Category` column being `SubD`. (`Axis Partner`/`Milestone Partner` were tried too in
  an earlier draft, then dropped once real data showed they're brand-partnership tiers, not a
  supplier signal — see `is_subdistributor_category()`. The Zoho step of that precedence, and the
  LinkedIn-title fallback for when neither source hints at it, aren't wired up yet —
  `infer_supplier_from_title()` exists and is tested, just not called from the harvester.)
- **`contacts.contact_class`** (`engaged`/`lead`/`backlog`, nullable): the Customers-branch
  signal-to-noise filter, now driven by both halves of the spec's trigger:
  - **Zoho**: `python -m src.zoho_activity import <export.xlsx>` records each contact's real Zoho
    "Last Activity Time" from an offline export (ahead of teaching the live API pull to request
    that field).
  - **Qreg**: it turned out to be the `QReg` sheet inside each rep's own Pentagon Quotation
    Template workbook, not a separate system — one row per quote, with a date and the customer's
    company/contact/email. `python -m src.qreg import <rep's .xlsm> [...]` records each matched
    contact's latest quote date.
  - Either `python -m src.zoho_activity classify` or `python -m src.qreg classify` then fills
    `engaged` for anyone still unclassified whose recorded activity/quote is inside
    `classification.engaged_activity_days` / `engaged_quote_days` (default 365 each) — run both,
    whichever signal is recent enough wins, since neither ever overwrites a contact someone (or
    the other signal) already classified. Edit manually via `POST /api/contacts/{id}`
    (`"contact_class": "engaged"`, or `""` to clear).
- **`contacts.reports_to_id`**: contact-level org chart, usable for Elvey's own staff and for any
  large customer's internal structure alike. `seed_data/elvey_org_chart.json` (+ the human-readable
  `elvey_org_chart.md`) is a one-time seed of Elvey's actual reporting line (EXCO down to branch
  level) — run `python -m src.orgchart seed` once to import it. Editable afterwards via `POST
  /api/contacts/{id}` (`"reports_to_id": <id>`, or `0` to clear).
- **`contacts.segment_override`**: pins a contact to one role when it differs from its account's.
  Readable from day one (`_effective_roles()` in phase 2 already honoured it); the ability to
  actually *set* it — API field + a "Role" picker on the contact card, shown only when the
  account carries more than one role — was a gap caught in the final pass and closed then.

### v4 phase 2 (2026-10-01): role-based tabs, Customers-branch scaling

- **Role-based tab membership.** The People Tree now has a **Suppliers** tab alongside
  Competitors/Internal/Customers. A dual-role account (a sub-distributor: a customer with
  `Installer Category = SubD`) shows under *both* its tabs with the same people, same account
  record — no duplicate data, just duplicate display. A contact's `segment_override` (once
  something sets it) pins them to one tab instead of inheriting every role their account carries.
- **Customers-branch scaling.** Every group sorts `engaged` contacts first, ahead of allocation and
  priority. The People Tree filter bar gained a **Classification** filter (All/Engaged/Lead/Backlog/
  Unclassified) and, on the Customers tab only, an **"Engaged & allocated only"** toggle chip — the
  default-view behaviour the spec calls for, but left as a one-click toggle rather than an actual
  default: real Zoho- and Qreg-activity classification are both wired up now (see phase 1 above),
  but together still only cover roughly a fifth of the real roster so far (every rep's own
  Exports/other-region book matches the current SA-focused sheet poorly — a scope gap, not a
  bug) — contacts outside that coverage show as unclassified, not wrongly excluded.

### v4 phase 3 (2026-10-01): the Analyze/BI view

A new **Analyze** tab (`src/analyze.py`, `/api/analyze`) alongside the People Tree: 13 small,
named aggregate panels — people by branch, accounts by role, allocated vs unallocated, by
category/classification/department/region, latest sellout by division and by rep, in-Zoho/in-
MakDB coverage, where each contact's role came from, and face+LinkedIn-summary coverage. No
generic query builder — every panel is its own auditable SQL query, same philosophy as
`classify_department`. Most slices are clickable: each carries a `filter` dict in the exact shape
the People Tree's filter bar already reads (`branch`/`fdept`/`fcat`/`fclass`/`frep`/`q`), so
clicking one just applies that filter and switches tabs — no second filtering mechanism to keep
in sync. The three coverage panels have no matching filter control yet and are informational
only. (Picked up a `frep` filter option while at it — `__alloc`, "Allocated only" — so the
Allocation panel's "Allocated" slice has something exact to drill into.)

### v4 phase 4 (2026-10-01): Table view + org chart

- **Table view.** A Cards/Table toggle in the People Tree's filter bar (`src/static/index.html`)
  — same filters, same branch tabs, just a different render. Table mode flattens everyone
  currently matching into one sortable grid (click a header to sort, click again to reverse) over
  16 possible columns (name, role, department, company, region, rep, category, classification,
  email, phone, LinkedIn, employment status, in-Zoho/in-MakDB, priority, reports-to); which ones
  show is checkbox-picked via a **Columns** popover and remembered in `localStorage` across
  reloads. **Export CSV** writes exactly the currently filtered/sorted/shown table, client-side.
- **Org chart.** A read-only indented tree, built client-side from each group's own `reports_to`
  field (no new endpoint needed — `/api/people` already carries it) — reuses face photos the same
  way cards do. Renders under any People Tree group that has at least one actual reporting line
  in it: Internal's contacts all land in one "(no account)" group once `python -m src.orgchart
  seed` has run, so the whole Elvey chart renders there; a customer account's chart fills in as
  its own contacts get a "Reports to" set.
- **Contact card** grew a "Reports to" / "Direct reports" display (click either to jump to that
  person's card) plus a "Reports to…" picker with a datalist scoped to the same group (same
  account, or Internal) — the normal case from phase 1 §3. The rare cross-account picker is still
  out of scope.
- Verified end-to-end with Playwright against a real Chromium build: table render/sort/column-
  persistence/CSV export, org-chart render, and the reports-to picker's set/clear round-trip —
  all against the real 62-person org-chart seed data from phase 1.

### v4 phase 5 (2026-10-01): exports

- **Export CSV** now shows in Cards mode too, not just Table — same client-side mechanism from
  phase 4, same filters, exactly what's currently shown in either view.
- **Quote export** (`src/export.py`, `POST /api/export/quote`): the named preset the spec called
  for — the exact Consolidated Sales Contacts column set (`Ref, Full Name, Role, Region/Branch,
  Office Tel, Cell number, Email Address, Linkedin, Company name, ACCNO, Axis Partner, Milestone
  Partner, Installer Category, AM, Category, In Zoho, In MakDB, Cluster, Sort`), written back out
  as a real `.xlsx` from the current DB state for whichever contacts are currently filtered in
  the dashboard. Server-side, because several of those columns (`ACCNO`, `Cluster`, `Axis
  Partner`, `Milestone Partner`, `Installer Category`, `Sort`, `Ref`) never reach the browser via
  `/api/people` at all — this pulls fresh from `accounts`/`contacts` instead of reshaping what the
  Table view already has. Shows next to Export CSV whenever the Customers or Suppliers branch is
  open. `Ref` has its ingest-time `REF:` prefix (`src/ingest.py`'s `source_id` convention)
  stripped back off, so it round-trips as the original sheet value.

### v4 final pass (2026-10-01): closing gaps against the spec

A re-read of `V4-SPEC.md` against the actual code after all five phases, catching a few things
that had been promised but not quite finished, or had gone stale in the spec's own write-up (see
`V4-SPEC.md`'s own "Final pass" section for the full list):

- `segments.supplier_token` (`SUPP`) actually exists now — a supplier contact's face file gets
  its own token instead of falling through to competitor-style per-company naming.
- `segment_override` can now actually be set (not just read): `POST /api/contacts/{id}` takes it,
  and the contact card grows a "Role" picker whenever the account carries more than one role.
- The Zoho screen's "Select all shown" now actually narrows to `engaged`+`allocated` contacts, as
  phase 5 was supposed to do — the diff item didn't carry those fields at all before this.
- The Table view's column list gained back `face` (optional, not default-on), matching the
  spec's original field list.

```
config.yaml        paths, scope, caps, Zoho field mapping
.env               Zoho credentials (copy .env.example; never committed)
src/schema.sql     accounts, contacts, harvest_log, zoho_records (mirror), zoho_sync_log
src/ingest.py      consolidation workbook -> DB
src/images.py      face library -> contacts, coverage, data/to_enrich.csv
src/harvest.py     LinkedIn face + summary + work history (Playwright, persistent profile)
src/orgchart.py    account_roles sync + the one-time org-chart reports_to_id seed (v4 phase 1)
src/analyze.py     named BI panels + drilldown filters for the Analyze tab (v4 phase 3)
src/export.py      the Quote-export preset: Consolidated Sales Contacts columns as .xlsx (v4 phase 5)
src/dashboard.py   FastAPI app + src/static/index.html
src/zoho.py        auth, pull, diff, push selected, photo upload, id write-back
src/zoho_activity.py  offline Zoho Contacts export -> Last Activity Time -> engaged classification
src/qreg.py        a rep's QReg sheet (in their quotation workbook) -> last quote -> engaged classification
seed_data/         one-time import sources (the Elvey org chart)
tests/             pytest suite (synthetic data only)
```

## Setup (once, on the machine with the `G:` drive)

```powershell
cd elvey-kyc
py -3.11 -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # or set harvest.browser_channel: chrome to use installed Chrome
copy .env.example .env             # then fill in the Zoho values (see "Zoho" below)
```

Edit `config.yaml`:

* `paths.kyc_folder` is the `Elvey KYC — People` Drive folder. It is already set to the brief's path.
* `inputs.workbook` is the consolidation output. The current build is **`Elvey Consolidated Relational Database 20260924.xlsx`**. It lives in the Drive `Sales` folder; `config.yaml` already points at it. Use the `.xlsx` file, not a `.gsheet` shortcut. Its sheets are `dim_accounts`, `dim_contacts`, `dim_reps`, `fact_sellout`, `view_top50_projects` and `view_top500_contacts`, and ingest reads them directly. A single flat CSV/xlsx (one row per contact) also works: headers are matched by alias (`Company`, `Contact Name`, `E-mail`, `Mobile`, `FY26 Sellout`, …).

You can also override paths through env vars: `KYC_FOLDER`, `KYC_WORKBOOK`, `KYC_DB`.

## Run order

```powershell
python -m src.ingest                 # load accounts + contacts, print counts / coverage
python -m src.images                 # match faces, write data/to_enrich.csv
python -m src.dashboard              # http://127.0.0.1:8765  (People Tree + Zoho sync)
python -m src.harvest --dry-run      # see the LinkedIn queue
python -m src.harvest --test         # 5-contact test (browser opens; log in the first time)
python -m src.harvest --report 5     # check what the test captured
python -m src.harvest                # full run: up to the daily cap (40); re-run each day to continue
python -m src.images                 # refresh coverage / to_enrich after harvesting
python -m src.zoho check             # auth + list of custom fields still missing in Zoho
python -m src.zoho pull              # read Zoho into the local mirror (read-only)
# review in the dashboard -> Zoho sync tab, or on the CLI:
python -m src.zoho diff --status new
python -m src.zoho push --select new             # dry run
python -m src.zoho push --select new --live      # live (after live_enabled: true)
python -m src.zoho log
```

### What ingest does with the relational workbook

* **Accounts**: the `view_top50_projects` rank, plus every account referenced by an in-scope contact. `rep` is the resolved `allocation_proposed` rep name from `dim_reps`. `latest_sellout` is FY26, falling back to FY25. `brand_focus` is the top 3 brands by sellout from `fact_sellout`. `allocation` is the allocation status, or `Conflict: SN / TJ` where two reps both claim the account. Category, Axis level, Tasha visit and concerns go into `extra`, and the dashboard shows them as key-tags.
* **Contacts**: the `view_top500_contacts` rank becomes `priority`. `in ›` LinkedIn placeholders are dropped. Customers who already have a KYC face are also loaded (`scope.include_kyc_faces`), so every existing JPEG matches someone.
* **Competitor and internal branches**: the seed rows in `dim_contacts` have no account. Ingest assigns them from the face-file token (`Duxbury`, `Reditron`) or to `Elvey`. Staff who also appear as "customers" of the `Elvey` account are merged into the Internal branch.
* **Dedupe**: accounts by account number, then normalised name (`(Pty) Ltd` and similar suffixes ignored). Contacts by email, then account + name. Re-running ingest never blanks a field and never touches enrichment or Zoho columns.

## LinkedIn harvester

* Playwright opens a **visible** Chromium window with its own persistent profile (`data/chrome-profile`). The first run pauses so you can log into LinkedIn by hand. LinkedIn usually asks a new browser to verify it's you (a code by email/SMS or a puzzle): complete that in the same window and press Enter in the terminal once you can see your feed. The harvester waits and never navigates away from that page. The login is remembered after that. No credentials are ever stored or typed by code. This profile is separate from your everyday Chrome profile, because Chrome can't share a profile directory while it's open.
* For each contact it does the following:
  1. It opens the stored LinkedIn URL. If there isn't one, it searches `"<Name> <Company>"` and takes the top result that contains the surname.
  2. On the profile it reads the headline and About section (the summary is capped at about 500 characters) and the first 3–5 Experience entries (title, company, dates).
  3. It takes the top-card photo, canvas-downscaled to 240 px at JPEG 0.85. It falls back to the chosen search card's photo, never another result's.
  4. It saves the photo as `<Segment>__<First>_<Last>.jpg` in the KYC folder.
* **The two Chrome download settings from the old approach are no longer needed.** Python writes the JPEG straight into the KYC folder. You only need those settings (allow multiple downloads for linkedin.com; Downloads → the KYC folder) if you capture faces by hand in your normal Chrome.
* **Safety.** There is a daily cap (`harvest.daily_cap`, default 40) and random 4–9 s pacing. The run stops at once on a checkpoint, authwall or "commercial use limit" page. If the top match's name doesn't match the contact, the result is recorded as `no_profile` and nothing is saved. A match is flagged `name-only` when the company doesn't appear on the profile, so you can verify it on the card.
* **Finding the right profile.** The search tries "name + company" first, then the name alone. It only accepts a result that shows the right company, or the one clear name match. With several same-name results and no company match it records *no LinkedIn profile* rather than guess. The person's name is read from the page heading, falling back to the browser tab title.
* **Check what was captured** with `python -m src.harvest --report 10`. It prints the role, department, employment status, summary and roles stored for the last 10 people.
* **Debug copies.** Anything that didn't work (no profile, nothing readable, an error) leaves a text copy of what LinkedIn showed in `data/debug/`.
* **Retry the misses** with `python -m src.harvest --retry`. It re-queues everyone without a summary yet: no profile found, failed, or came back empty. People marked *left* or *not relevant* are never re-queued.
* **Diagnose one person** with `python -m src.harvest --diagnose "Josslyn Abdull"`. It looks them up, prints exactly what was read and saves the page text to `data/debug/diagnose_<id>.txt`, without changing the database. Send that file if something looks wrong.
* **Departments** (Management, Sales, Technical, Projects, Procurement, Finance, Marketing, Operations, IT, HR, Admin) are guessed from the job title. Sources in order of preference:
  1. a department you set by hand (never overwritten);
  2. the current LinkedIn title;
  3. the spreadsheet role;
  4. a role mailbox such as `accounts@`.

  `dashboard.relevant_departments` in `config.yaml` sets which departments count as sales-relevant. Others get a *not sales* tag.
* **Moved on?** If a person's current LinkedIn role is at a different company, they're flagged *moved* with the new employer, and the matching account is suggested when there is one. Nothing is re-allocated automatically. Pick **Needs review** in the dashboard's status filter and decide per person:
  - **Move to <new account>**, or add the new company as an account and move them there;
  - **Mark as no longer at <company>**;
  - **LinkedIn is wrong – keep**.

  Past employers are kept on the card.
* **Contact card.** Click any person to see:
  - email, phone and LinkedIn;
  - the full summary and roles, department, and KYC status;
  - for customers, the account's rep, division, sellout, brand focus and allocation.

  You can also set the department and status (active / no longer at company / not relevant) with a note, or move the person to another account. People who left or aren't relevant are hidden unless you pick that status in the filter, and they're left out of the Zoho push list.
* **Location.** When LinkedIn shows one, the top-card location line ('Johannesburg, Gauteng, South Africa') is split into city, province and country and stored on the contact (shown on the card as "from LinkedIn"). The consolidation workbook has no per-contact location, so this only ever comes from LinkedIn. In Zoho these map to the standard `Mailing_City` / `Mailing_State` / `Mailing_Country` fields — Zoho's own label for that field is "State", but it holds what we call the province.
* **Self-check.** Run `python -m src.harvest --verify` any time (no LinkedIn needed) to scan every
  contact already harvested and flag:
  - a stored summary that's actually LinkedIn's page footer (the bug above, in case it slipped
    through before this fix);
  - marked done but nothing usable was captured;
  - a summary with no role or work history;
  - employment status that never resolved despite having data;
  - two different contacts pointing at the same LinkedIn profile (a likely mismatch — always
    reported, never auto-fixed).

  The first two are unambiguous, so they're fixed automatically by default: the bad summary is
  cleared and the contact is re-queued for another try — run a normal harvest afterwards to refresh
  them. Add `--no-fix` to only see the report. `python -m src.harvest` also runs this check quietly
  at the start of every normal run, so bad old data never just sits there unnoticed. The dashboard
  shows the same check as a banner across the top of the People Tree, with a "Fix what can be
  fixed" button.
* **A fixed bug:** when a profile hadn't finished rendering a `<main>` landmark, the reader fell back to the whole page, which includes LinkedIn's footer links (Accessibility, Talent Solutions, Community Guidelines, ...). That footer could be mistaken for the profile's own About section and saved as the summary. It's now stripped before anything is read, and a profile that turns out to have nothing readable is queued for a retry instead of being marked done.
* **Phone numbers** are standardised on ingest to international format:
  - South Africa: `+27 82 659 7188`;
  - other countries: `+267 71 729 638`.

  This also fixes numbers that lost their leading 0 in Excel. The original value is kept on the record, and anything that can't be parsed is left as entered and flagged.
* **Misses are normal.** Expect roughly 80–90 % success for Elvey staff and larger competitors, and 10–30 % for small resellers. They are stored as `no_photo` / `no_profile` and not retried. Failures are retried up to `max_attempts` times.
* The selectors live in `JS_TOP_RESULT` / `JS_PROFILE` / `PHOTO_SELECTOR` in `src/harvest.py`. `tests/test_harvest_js.py` runs them in real Chromium against mock markup. If LinkedIn changes its layout, update them there.

> **LinkedIn ToS:** LinkedIn's User Agreement restricts automated access. Keep this modest, interactive and owner-run: your own account, small daily volumes, for Elvey's own KYC. Stop if LinkedIn warns you.

### Scheduling the harvester (so you don't have to run it by hand)

`python -m src.harvest` already paces itself (`harvest.delay_min_s`/`delay_max_s` between contacts)
and stops the moment it hits the daily cap or a LinkedIn checkpoint/authwall — the pieces needed to
run it unattended are already there. `scripts/run_harvest.ps1` wraps one call and appends its
output to `data\harvest_schedule.log`; schedule *that* to fire every couple of hours during the day
instead of running the whole day's cap in one sitting, which both looks more human to LinkedIn and
means a checkpoint only costs you that one batch, not the rest of the day.

One-time setup, in PowerShell from the repo folder:
```
schtasks /Create /SC MINUTE /MO 120 /TN "Elvey LinkedIn harvest" /TR "powershell -ExecutionPolicy Bypass -File \"$PWD\scripts\run_harvest.ps1\" -Limit 8" /RL LIMITED
```
This runs a batch of up to 8 contacts every 2 hours, around the clock (five such batches a day
roughly matches the default `harvest.daily_cap` of 40 — raise `-Limit`/the cap, or add more, to
taste). It needs nothing from you after that, but it does need:
- **You logged in once already** (`python -m src.harvest --test` once, interactively, to get past
  LinkedIn's login/security check in the visible browser window) — the persistent profile in
  `data/chrome-profile` remembers that session.
- **A visible desktop session** for the scheduled runs to open their browser window in, same as a
  manual run (headless is not recommended — see above). In Task Scheduler's properties for the task,
  under *General*, tick "Run only when user is logged on"; the window will briefly pop up each run.
  If the task instead needs to run while you're logged out, this harvester isn't set up for that —
  say so and we can look at headless as a fallback, accepting the higher block risk that implies.

Check on it any time with `Get-Content data\harvest_schedule.log -Tail 40` or
`python -m src.harvest --report 10`. If a batch stops early with "not logged in" or a checkpoint
message in the log, LinkedIn needs you to re-verify by hand — run `python -m src.harvest --test`
once, interactively, then the scheduled runs pick back up on their own.

To pause it: `schtasks /Change /TN "Elvey LinkedIn harvest" /DISABLE` (`/ENABLE` to resume). To
remove it entirely: `schtasks /Delete /TN "Elvey LinkedIn harvest" /F`.

Running this unattended is a step beyond "interactive, owner-run" — you're still the only account
being used, and the existing cap/pacing/stop-on-checkpoint behavior all still apply, but nobody is
watching each run happen. Keep an eye on the log for the first few days and back off the schedule if
LinkedIn ever pushes back.

## Correcting a specific person (the Corrections chat)

The dashboard's **Corrections** tab is the fastest way to work through the handful of people each
harvest run couldn't resolve on its own: a search that found nobody, a match by name only, or two
people pointing at the same LinkedIn profile — worst-blocked first. It shows one person at a time
with the reason, a one-click LinkedIn search link, and a plain-text box. Type the correction and
press Enter; it applies immediately and moves to the next one:

- paste a LinkedIn profile URL to pin their exact profile (skips search for them from then on);
- `left`, `resigned`, `no longer there` — marks no longer at the company;
- `not relevant` — marks not relevant;
- `active`, `keep`, `that's right` — confirms them, or undoes a flagged move if LinkedIn was wrong;
- a department name, or `department: sales`;
- `now at <company>` / `moved to <company>` — re-allocates to that account, or offers to add it;
- `skip` moves on without changing anything; `stop` pauses.

This is a small parser over the same fields the contact card already writes — not a live AI chat,
nothing new to configure, no external call. If it doesn't understand a line it says so and asks
for one of the above rather than guessing. The same queue and logic are behind
`python -m src.harvest --verify` and `--set-url`, so the two never disagree.

**The CLI equivalent**, useful when you already know exactly what you want to fix without opening
the dashboard:

```powershell
python -m src.harvest --set-url "Marie Deysel" "https://www.linkedin.com/in/marie-deysel-1503a53a/"
```

Same effect as typing the URL into the chat or the contact card: pins the exact profile, re-queues
for the next harvest run. A wrong or malformed URL is rejected outright; nothing is touched.

**What gets flagged for you instead of guessed automatically:** a contact matched by name only,
with nothing on the profile confirming the company, shows "matched by name only — please verify"
on its card and in `--verify`'s `unsure_match` list. Two different contacts pointing at the same
LinkedIn profile are flagged the same way. Neither is ever auto-corrected — that needs a person to
look at the actual page — but everything else (footer pollution, a result marked done with nothing
usable) is fixed automatically so bulk runs don't need babysitting.

## Zoho CRM

**Org.** Pentagon Distributors (South Africa, Enterprise edition). The team logs in at `crm.zoho.com`, so the datacenter is **`ZOHO_DC=com`** (accounts: `accounts.zoho.com`, API: `www.zohoapis.com`). This is the default in `.env.example`.

**Credentials.** Create them at https://api-console.zoho.com → *Self Client*.

1. Generate a grant code with scopes `ZohoCRM.modules.accounts.ALL,ZohoCRM.modules.contacts.ALL,ZohoCRM.settings.fields.READ`.
2. Exchange the code for a refresh token:
   ```
   POST https://accounts.zoho.<dc>/oauth/v2/token?grant_type=authorization_code&client_id=…&client_secret=…&code=…
   ```
3. Put the client id, client secret and refresh token into `.env`.

**Custom fields.** These must be created in the Zoho UI once; on this plan the API can't create them. On 2026-09-24 only `Pentagon_Account_Number` existed (mapped from `accno`). All of the following were missing:

| Module | Field label → API name | Type |
|---|---|---|
| Accounts | Division, Cluster, Branch, Elvey Rep, Brand Focus, Allocation, KYC Status | Single Line |
| Accounts | Latest Sellout | Currency |
| Accounts | Rank | Number |
| Accounts | LinkedIn URL | URL |
| Contacts | LinkedIn URL | URL |
| Contacts | LinkedIn Summary | Multi Line (Large) |
| Contacts | Segment | Single Line |

To add them: Setup → Customization → Modules and Fields → *module* → Standard layout, then drag in the fields. Zoho derives the API name from the label (`Elvey Rep` → `Elvey_Rep`). `python -m src.zoho check` prints what's still missing. Until a field exists, it's **skipped** on push, so pushes still work with only the standard fields (Account_Name, First/Last_Name, Email, Phone, Mobile, Title).

**How the sync behaves**

* **pull** reads Accounts and Contacts (paged, 200 per call, with `page_token` beyond 2,000 records) into `zoho_records`. It never writes to Zoho.
* **diff** matches local records to Zoho records:
  * accounts by stored Zoho id → Pentagon account number → normalised name;
  * contacts by stored Zoho id → email → account + name.

  Each record is then `new`, `changed` (with a field-level list) or `in-sync`. Local blanks never count as changes, so a push can't wipe data in Zoho. Photos count as pending when there's a local face that hasn't been uploaded yet.
* **The review screen** (dashboard → Zoho sync) has a checkbox per record, and per field in the expanded row, plus a photo checkbox. **Preview push** is always a dry run and shows the exact payloads. **Confirm LIVE push** appears only when `zoho.live_enabled: true` is set in `config.yaml`, and you must type `PUSH`.
* **push** handles accounts first, then contacts, then photos. It uses create/update batches of up to 100. When a new contact's account isn't in Zoho yet, that account is included automatically. Zoho ids and timestamps are written back, and every action goes to `zoho_sync_log`. An in-sync record you select is simply *linked*: its Zoho id is stored and no API call is made. After a push, those records show as in-sync without a re-pull.
* Credit-friendly behaviour: a 0.3 s pause between calls, and back-off on 429/5xx responses.

**First live push:** run a dry run, review it, then set `live_enabled: true`.

### The full LinkedIn run

The harvester works through everyone who still needs a photo or a summary, best priority first: competitors and internal staff, then customers by rank. It stops at the daily cap, and each daily run carries on where the last one stopped.

```powershell
python -m src.harvest --retry      # once: re-queue the test people that came back without a profile
python -m src.harvest              # up to 40 people; leave the window alone while it runs
python -m src.images               # refresh coverage + KYC status
python -m src.dashboard            # review: Needs review (moved), departments, cards
```

Repeat `python -m src.harvest` once a day. About 516 people at 40 a day is roughly two weeks of short runs. You can narrow a run with `--segment internal|competitor|customer` or `--limit 20`. Raising the cap with `--cap 60` is possible, but LinkedIn is more likely to show a security check at higher volumes.

## Growing past 500

Nothing is hard-capped except the settings in `config.yaml`:

* Set `scope.top_accounts` / `scope.top_contacts` to `null` (or run `python -m src.ingest --all`) to load all ~960 accounts and ~1,500 contacts in the current workbook.
* Harvesting is driven by the daily cap: 1,000 new contacts at 40 a day takes about 5 weeks of short sessions. Raise the cap cautiously.
* Zoho pull and push are already paged and batched, and the dashboard renders large lists in pages of 200.

## Tests

```
pytest -q
```

The fixtures are a synthetic workbook with the same layout as the real one, a fake Zoho, a fake LinkedIn driver, and real Chromium against mock markup for the in-page JavaScript. No real customer data is committed. `.gitignore` excludes `*.xlsx`, `*.db`, `.env`, `data/` and the browser profile.
