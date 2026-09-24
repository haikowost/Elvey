# Elvey KYC — lightweight CRM + Zoho sync

A local, resumable KYC tool for Elvey Group's Projects/Pentagon division. It:

1. **Ingests** the Customer Data Consolidation output: the top 50 accounts, the top 500 contacts, and the competitor and internal branches.
2. **Matches** the KYC face library (`<Segment>__<First>_<Last>.jpg`) to contacts.
3. **Enriches** contacts from LinkedIn: a face, a short summary and the last 3–5 roles. It drives your own logged-in browser, with a daily cap and a checkpoint after every contact.
4. Serves a **local dashboard**: the People Tree (Competitors / Internal / Customers) and a Zoho **review-and-select** screen.
5. **Syncs to Zoho CRM**. It first pulls Zoho and diffs it against the local DB (new / changed / in-sync). You pick the records and fields to send. It pushes only those, uploads photos, writes the Zoho IDs back and logs every write.

Everything lives in one SQLite file (`elvey_kyc.db`). Every command can be re-run safely.

```
config.yaml        paths, scope, caps, Zoho field mapping
.env               Zoho credentials (copy .env.example; never committed)
src/schema.sql     accounts, contacts, harvest_log, zoho_records (mirror), zoho_sync_log
src/ingest.py      consolidation workbook -> DB
src/images.py      face library -> contacts, coverage, data/to_enrich.csv
src/harvest.py     LinkedIn face + summary + work history (Playwright, persistent profile)
src/dashboard.py   FastAPI app + src/static/index.html
src/zoho.py        auth, pull, diff, push selected, photo upload, id write-back
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
