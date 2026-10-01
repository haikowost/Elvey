# Elvey KYC v4 — the visual & BI address book

Scope note for the next rebuild phase. Supersedes nothing in README's v3 section (that work ships
first); this is what comes after. Not yet built — this is the spec to react to before any code
changes, same pattern as every other round in this project.

## Vision

Elvey KYC stops being "a sales-contact sync helper" and becomes the company's **visual and BI
address book**: every person and organisation Elvey deals with — customers, suppliers,
competitors, and Elvey's own staff — in one local tool, browsable visually (the existing People
Tree) and analytically (new BI drilldown), with a deliberately curated, reviewed sync of only the
*relevant* contacts to Zoho. The customer side is and will stay the largest branch, so the tool's
usability at that scale is the main design constraint, not an afterthought.

## What stays as-is

- **Local-first, single SQLite file, single user, no auth.** Nothing here changes that.
- **The People Tree's branch structure** (Competitors / Internal / Customers, + Suppliers below) —
  still the primary visual browsing surface. This spec adds a BI layer and better scaling for the
  Customers branch; it doesn't replace the tree.
- **Zoho stays pull-diff-review-select-push.** No live write without an explicit, reviewed
  selection — this spec only tightens what's *suggested* for selection by default.

## 1. Multi-role accounts (an account can be more than one thing)

**Problem:** some accounts — notably sub-distributors — are a customer *and* a supplier. Today
`segment` is a single value on both `accounts` and `contacts` (`competitor` | `internal` |
`customer`), so an account can only live in one People Tree branch.

**Design:**
- New table `account_roles (account_id, role)`, many-to-many, `role` in
  (`customer`, `supplier`, `competitor`, `internal`). An account can carry more than one row.
- `accounts.segment` stays as the *primary* role (what it was created as, used for simple
  filtering/back-compat); `account_roles` is the authoritative set an account actually belongs to.
- The People Tree's branch tabs become **role membership**, not a strict single-branch column: an
  account with both `customer` and `supplier` roles appears under both tabs, same account record,
  no duplication.
- Contacts normally inherit the account's roles. A contact who is specifically the supplier-side
  contact at an otherwise-customer account gets `contacts.segment_override` (nullable) to pin them
  to one role without re-tagging the whole account.
- **Suppliers**: no data exists yet. This design reserves the `supplier` role/token now (naming
  convention gets a `SUPP` face-filename token alongside `CUST`/`INT`/the competitor tokens) so
  ingesting real supplier contacts later is just "load the sheet," not a schema change.
- **Confirmed: a real `account_roles` table**, not a simpler JSON tag — settles open question 1.
- **Where an account's role comes from, when it isn't given directly** — a three-step precedence,
  same shape as the existing `role_source`/`ROLE_RANK` pattern: **Zoho** (an account's module/type
  there — still not wired up, no account-level type signal in what's been pulled so far) → the
  **consolidated contact sheet** → as a last resort, **harvest LinkedIn's current job title** and
  keyword-match it (installer/distributor/reseller-shaped titles tag the account `supplier`).
  Each step only fills what the step before it left blank; a Zoho or sheet-asserted role is never
  overwritten by a LinkedIn guess.
  **Corrected against the real sheet:** only `Installer Category` actually hints at a supplier
  relationship, and only one specific value — `SubD` (sub-distributor) means the account also
  resells, i.e. is itself a supplier. `Axis Partner`/`Milestone Partner` turned out to be brand-
  partnership tiers (`Authorized`/`Silver`/`Gold`/...) — about the account's own standing with that
  brand, nothing to do with whether *they* supply *Elvey*. An earlier draft of this heuristic used
  all three; `is_subdistributor_category()` in `src/util.py` is the fixed version.

## 2. Contact classification — what actually makes the Customers branch manageable

**Problem:** importing every contact in the consolidated sheet at face value means the Customers
branch is mostly noise — unverified, un-engaged, unprioritised. "No point in importing a whole lot
of unverified, un-sanitised contacts" (your words last round) is the design constraint.

**Design:** a new `contacts.contact_class`:
- **`engaged`** — a real, current CRM contact the team is actively working. This is the clean CRM
  core. Only `engaged` (+ `allocated`) contacts are eligible for the Zoho push's default selection.
- **`lead`** — a potential new sales contact, not yet engaged. Surfaced as its own actionable list
  (think: a sales-outreach queue), not mixed into the engaged view.
- **`backlog`** — relevant to a different department/business unit, out of scope for this phase.
  Parked, visible on request, never in the default view. This is exactly the "other contacts
  relevant to other departments... loaded accordingly in future phases" bucket.
- Unclassified (`NULL`) is the ingest default until someone (a rule or a human) classifies it —
  never silently treated as `engaged`.

**Scaling the Customers branch:** the default People Tree view for Customers becomes "`engaged`
and `allocated`, grouped by account" — everything else (leads, backlog, unallocated) is one filter
click away, not scrolled past. This is the actual fix for "the dashboard doesn't look great" at
scale: it's not a styling problem, it's a signal-to-noise problem, and classification is the fix.

**Where the classification comes from (confirmed, settles open question 2):** auto-classify
`engaged` from actual activity, not just allocation — a contact with recent logged activity in
**Qreg** (the quote register) or in **Zoho** (a deal, task, or note against them) is `engaged`;
everyone else starts unclassified. This is a stronger signal than the "allocated + resolved role"
rule drafted earlier (dropped) — allocation says who's *responsible*, activity says who's actually
*being worked*, which is the real "no unverified contacts" test. Plus the same manual override from
the dashboard contact card (a dropdown next to the existing department/status ones), for anyone the
activity signal hasn't caught up with yet. No LLM/AI classification; same "small, auditable rules"
philosophy as `classify_department`.

**Zoho half: done.** `src/zoho_activity.py` imports an offline Zoho Contacts export (ahead of
teaching the live API pull to request `Last Activity Time`, which it doesn't today) and fills
`contact_class='engaged'` for anyone unclassified whose recorded activity is inside
`classification.engaged_activity_days` (default 365) — never touching a contact already
classified by anything else. Matches by email, then account + name, then name alone when unique.

**Qreg half: also done.** Found: Qreg isn't a separate system — it's the `QReg` sheet inside each
rep's own Pentagon Quotation Template workbook (`MASTERv12`/revisions), one row per quote raised,
with a quote date and the customer's company/contact/email. (What first arrived labelled "Qreg
data" was actually the refreshed Consolidated Sales Contacts roster — valuable on its own, now
the real ingest source, but a roster, not a quote log; the real Qreg sheets came in a follow-up.)
`src/qreg.py import <rep's .xlsm> [...]` reads one or more reps' sheets and records each matched
contact's latest quote date (`contacts.qreg_last_quote`); `python -m src.qreg classify` fills
`engaged` the same fill-blank-only way as the Zoho half. Run both — whichever signal is recent
enough wins, since neither ever overwrites an existing classification. Matching coverage varies a
lot by rep (65–109/121–137 for the SA-focused reps, 15/155 for the Exports rep) — the low numbers
are a scope mismatch, not a bug: that rep's book is almost entirely non-SA accounts (Botswana,
Namibia, Mozambique, Zambia, Nigeria, Zimbabwe...) not yet in this consolidated sheet's scope.

## 3. Contact-level hierarchy — within *any* account, not just Elvey's own

**Correction from an earlier draft of this spec:** the hierarchy that matters here is
**contact-to-contact**, not account-to-account. The original ask was "if there is a hierarchy
within the larger accounts this needs to be visible" — meaning a big customer's own contacts
(sales account manager, technician, pre-sales, ...) reporting up through a regional/divisional
manager to a national manager, all *within that one customer account*. It is the same shape as
Elvey's own internal reporting line, just not limited to the Internal branch.

**Design:** one mechanism, used everywhere: `contacts.reports_to_id` (self-referential FK to
`contacts.id`). Nothing auto-derives it — there's no source column for it in any sheet today — so
phase 1 adds a manual "Reports to" picker on the contact card (same pattern as the existing "move
to account" picker; the picker lists other contacts *at the same account*, since that's the
normal case, with an option to pick across accounts for the rare cross-account case). A contact
card and the account group view both grow a read-only org-chart rendering (an indented tree,
reusing the existing face photos) built from `reports_to_id` — under Internal for Elvey's own
staff, and under any large customer account for its internal structure. Real reporting-line data
can backfill later from an HR export or a customer-supplied org chart if either ever exists; the
manual path means it's usable from day one regardless.

**Account-to-account hierarchy** (a holding company with subsidiaries, or one customer with
multiple branch/site accounts) is a separate, smaller thing and only worth building if it's
actually needed — `accounts.parent_account_id` would cover it, but nothing in what's been asked
for so far requires it. Dropped from phase 1 unless you tell me otherwise.

**Seed data found (settles open question 3):** a real org chart already exists — EXCO down to
branch/cluster level (CEO → Finance/Sales-Marketing/Global-Comms directors → cluster leads/PMs →
AMs, branch managers, technicals — vacancies and proposed moves included). Captured as
`seed_data/elvey-org-chart.md` in this repo. Phase 1's schema migration includes a one-time seed
import of this as the initial `reports_to_id` chains for Internal-branch contacts: match each name
against existing Internal contacts (create a stub contact for a name that isn't there yet; flag
anything ambiguous for manual linking rather than guessing). Nothing on the customer side has
reporting-line data yet, so every customer account's hierarchy still starts blank, built manually
from the contact card as before. The chart's "proposed move"/"semi-retired" annotations are
presentational context for this one import, not a new contact field — only the current, as-is
reporting line gets written to `reports_to_id`.

## 4. BI drilldown + configurable views

The biggest actual engineering piece, and new to this tool rather than an extension of something
existing.

- **A new "Analyze" view**, separate from the People Tree. A set of aggregate panels: counts by
  segment/role, by `allocated` vs not, by `category`, by department, by region/branch, by
  `contact_class`, `in_zoho`/`in_makdb` coverage, face/LinkedIn-summary coverage %, role-pending %,
  sellout totals by division/rep. Each panel is a small, named SQL aggregate — no generic query
  builder, so it stays auditable and fast on a local SQLite file.
- **Drilldown**: clicking a slice of any panel (e.g. "Unallocated: 140") filters a contact list to
  exactly that slice — same filter mechanism the People Tree already uses, just driven from a
  clicked stat instead of a dropdown.
- **A Table view**, as an alternative to the card-based People Tree for this purpose specifically:
  cards are good for visual browsing, bad for bulk BI work. The table is dense, sortable, and the
  columns shown are **user-configurable** (persisted in the browser's `localStorage` — this is a
  single-user local tool, no server-side preference store needed) from the full field set: name,
  role, department, company, region/branch, allocated rep, category, contact class, email, phone,
  LinkedIn, employment status, in-Zoho/in-MakDB, face thumbnail. CSV export of the current
  filtered/column set, since "choose what data is shown" implies people will want to take it
  elsewhere too.

## 5. The master DB stays the single source of truth, with multiple export destinations

The SQLite file is, and stays, **the master record** — every other system (Zoho, a quote, a
report) is a derived, point-in-time export *from* it, never the other way round. Nothing reads
this tool's data back out of Zoho or a spreadsheet and treats it as more authoritative than the
local DB. That's already true for Zoho (pull → diff → review → select → push); this section
generalises the same shape to two more destinations.

- **Zoho sync** — unchanged mechanism (pull → diff → review → select → push, `live_enabled` +
  typed confirmation). The only change from this spec: the dashboard's "select all shown" default
  narrows to `contact_class='engaged' AND allocated=1`. Leads and backlog are never pre-selected —
  pushing them, if ever wanted, is still a deliberate individual selection, not a side effect of
  "select all."
- **Quote template export** — confirmed simpler than first assumed: it's the **same column
  layout as the consolidated contact list** (`Ref, Full Name, Role, Region/Branch, Office Tel,
  Cell number, Email Address, Linkedin, Company name, ACCNO, Axis Partner, Milestone Partner,
  Installer Category, AM, Category, In Zoho, In MakDB, Cluster, Sort`) — the same shape ingest
  already reads, just written back out from the current DB state (including anything the harvester
  or a manual edit has since filled in), for whatever account/contacts are currently
  filtered/selected. No bespoke template to map against — this is a round-trip export, not a new
  format.
- **Ad-hoc reports/exports** — generalises the Table view's CSV export (section 4) into a proper
  first-class action: whatever is currently filtered — a People Tree branch filter, an Analyze-view
  drilldown slice, a saved Table-view column set — can be exported (CSV, and XLSX where formatting
  matters) from wherever it's shown, not just from the Table view specifically. The Quote export
  above is really just one named **preset** of this same mechanism (the consolidated-list column
  set); any other column set is exportable the same way.

## Build phases

Kept small and reviewable, same pattern as every round so far — nothing here ships as one giant PR.

1. ✅ **Schema + data foundation.** `account_roles`, `segment_override`, `reports_to_id`,
   `contact_class` — additive migrations only, plus the ingest-time default-class rule. No UI
   beyond what's needed to verify it in the DB/API.
2. ✅ **Customers-branch scaling.** Role-based tab membership — a dual-role account (customer +
   supplier, from the sheet-hint inference) now shows under both branch tabs with the same
   people, until `segment_override` starts splitting individuals. The Customers branch sorts
   `engaged` contacts first within each group; a one-click "Engaged & allocated only" toggle and a
   classification filter are there for when contacts start actually getting classified (nothing
   does yet — Qreg/Zoho activity isn't wired up — so this doesn't default to hiding everyone on
   today's unclassified data).
3. ✅ **Analyze/BI view.** 13 named panels (`src/analyze.py`): branch, account role, allocation,
   category, classification, department, region/branch, sellout by division/rep, in Zoho/in
   MakDB coverage, role-source coverage, face+summary coverage. Most are clickable — each slice
   carries a `filter` dict in the exact shape the People Tree's own filter bar already reads, so
   drilldown is "apply this filter and switch tabs," not a second filtering mechanism. A few
   (the three coverage panels) have no matching filter control yet and are informational only.
4. ✅ **Table view + org chart.** A Cards/Table toggle next to the People Tree's existing filter
   bar (same filters, same branch tabs — no second filtering mechanism). Table mode is a flat,
   sortable grid over 16 fields; which columns show is checkbox-picked and persisted in
   `localStorage`, with a CSV export of exactly what's currently filtered/sorted/shown. The
   read-only org-chart rendering (an indented tree from `reports_to_id`, reusing face photos)
   appears under each People Tree group that actually has one — Internal's single "(no account)"
   group after the org-chart seed import, or any customer account once its contacts get manual
   reporting lines. The contact card grew a "Reports to"/"Direct reports" display plus a picker
   to set/clear the manager — scoped to contacts in the same group (same account, or Internal),
   matching the normal case from §3; the "rare cross-account case" picker is still out of scope.
5. **Exports.** Generic filtered CSV/XLSX export (from People Tree, Analyze, and Table views
   alike) with the consolidated-list column set as the Quote-export preset — both ship together,
   same mechanism.
6. **Suppliers go live** once real supplier data exists to ingest — schema's already there from
   phase 1, this is just "point ingest at the sheet."

## Explicit non-goals (for now)

- No multi-user accounts, permissions, or server deployment — still one person, one local SQLite
  file.
- No AI/LLM-based contact classification — rule + manual override, same philosophy as the existing
  department classifier.
- No *ongoing* automatic org-chart derivation — the existing Elvey org chart is a one-time seed
  import (section 3), not a live sync; every reporting line after that, and every customer-side
  one from day one, is manually entered.
- No change to the Zoho write-safety model (`live_enabled` + typed confirmation) — the sync gets a
  smarter default selection, not a new permission to write more.

## Open questions

All three resolved — see sections 1 (account-role precedence), 2 (Qreg/Zoho activity
classification), and 3 (org-chart seed import) above. Phase 1 (schema + data foundation) can
start: `account_roles`, `segment_override`, `contact_class`, `reports_to_id` plus the org-chart
seed migration, and the Zoho/sheet/LinkedIn account-role precedence. Qreg's actual shape (what
system, what export/API) still needs nailing down as the first concrete step inside phase 1 — not
a blocker to starting, since manual classification covers the gap until it's wired up.
