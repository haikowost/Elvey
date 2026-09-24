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
* `inputs.workbook` is the consolidation output. The current build is **`Elvey Consolidated Relational Database 20260924.xlsx`**. Point this at wherever Drive syncs it on `G:`. Its sheets are `dim_accounts`, `dim_contacts`, `dim_reps`, `fact_sellout`, `view_top50_projects` and `view_top500_contacts`, and ingest reads them directly. A single flat CSV/xlsx (one row per contact) also works: headers are matched by alias (`Company`, `Contact Name`, `E-mail`, `Mobile`, `FY26 Sellout`, …).

You can also override paths through env vars: `KYC_FOLDER`, `KYC_WORKBOOK`, `KYC_DB`.

## Run order

```powershell
python -m src.ingest                 # load accounts + contacts, print counts / coverage
python -m src.images                 # match faces, write data/to_enrich.csv
python -m src.dashboard              # http://127.0.0.1:8765  (People Tree + Zoho sync)
python -m src.harvest --dry-run      # see the LinkedIn queue
python -m src.harvest --test         # 5-contact test (browser opens; log in the first time)
python -m src.harvest                # up to the daily cap (40); re-run tomorrow to continue
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

* Playwright opens a **visible** Chromium window with its own persistent profile (`data/chrome-profile`). The first run pauses so you can log into LinkedIn by hand. No credentials are ever stored or typed by code. This profile is separate from your everyday Chrome profile, because Chrome can't share a profile directory while it's open.
* For each contact it does the following:
  1. It opens the stored LinkedIn URL. If there isn't one, it searches `"<Name> <Company>"` and takes the top result that contains the surname.
  2. On the profile it reads the headline and About section (the summary is capped at about 500 characters) and the first 3–5 Experience entries (title, company, dates).
  3. It takes the top-card photo, canvas-downscaled to 240 px at JPEG 0.85. It falls back to the chosen search card's photo, never another result's.
  4. It saves the photo as `<Segment>__<First>_<Last>.jpg` in the KYC folder.
* **The two Chrome download settings from the old approach are no longer needed.** Python writes the JPEG straight into the KYC folder. You only need those settings (allow multiple downloads for linkedin.com; Downloads → the KYC folder) if you capture faces by hand in your normal Chrome.
* **Safety.** There is a daily cap (`harvest.daily_cap`, default 40) and random 4–9 s pacing. The run stops at once on a checkpoint, authwall or "commercial use limit" page. If the top match's name doesn't match the contact, the result is recorded as `no_profile` and nothing is saved. A match is flagged `name-only` when the company doesn't appear on the profile, so you can verify it on the card.
* **Misses are normal.** Expect roughly 80–90 % success for Elvey staff and larger competitors, and 10–30 % for small resellers. They are stored as `no_photo` / `no_profile` and not retried. Failures are retried up to `max_attempts` times.
* The selectors live in `JS_TOP_RESULT` / `JS_PROFILE` / `PHOTO_SELECTOR` in `src/harvest.py`. `tests/test_harvest_js.py` runs them in real Chromium against mock markup. If LinkedIn changes its layout, update them there.

> **LinkedIn ToS:** LinkedIn's User Agreement restricts automated access. Keep this modest, interactive and owner-run: your own account, small daily volumes, for Elvey's own KYC. Stop if LinkedIn warns you.

## Zoho CRM

**Org.** Pentagon Distributors (South Africa, Enterprise edition). Confirm the datacenter by checking the domain you log in on: `crm.zoho.com` → `ZOHO_DC=com`, `crm.zoho.eu` → `eu`, and so on.

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
