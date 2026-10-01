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

**Where the classification comes from (phase 1):** a rule of thumb at ingest time — `allocated=1`
and `role_source` resolved → defaults to `engaged`; everyone else defaults to unclassified — plus a
manual override from the dashboard contact card (a dropdown next to the existing department/status
ones). No LLM/AI classification in this phase; same "small, auditable rules" philosophy as
`classify_department`.

## 3. Account hierarchy (parent/subsidiary, branch/site)

`accounts.parent_account_id` (self-referential FK). Covers both shapes the business actually has:
a holding company with subsidiaries, and one customer with multiple branches/sites. The People
Tree nests child accounts under their parent instead of listing every site as a flat, unrelated
group. Contacts stay attached to the specific account/site they belong to; the parent is a grouping
device, not a contact container.

## 4. Elvey's own org chart

We already hold most of what this needs: `contacts.segment='internal'`, `org_group`, and (from the
relational ingest path) each rep's `role`/`cluster`/`branch`. What's missing is the *reporting
line*. Add `contacts.reports_to_id` (self-referential FK to `contacts.id`). Nothing auto-derives
this — there's no source column for it yet — so phase 1 adds a manual "Reports to" picker on the
contact card (same pattern as the existing "move to account" picker) and a read-only org-chart
rendering under the Internal branch (an indented tree, reusing the existing face photos). Real
reporting-line data can backfill later from an HR export if one ever exists; the manual path means
it's usable from day one regardless.

## 5. BI drilldown + configurable views

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

## 6. Zoho sync — tightened default, not a new mechanism

The existing pull → diff → review → select → push flow is unchanged. The only change: the
dashboard's "select all shown" default narrows to `contact_class='engaged' AND allocated=1`. Leads
and backlog are never pre-selected — pushing them, if ever wanted, is still possible but has to be
a deliberate individual selection, not a side effect of "select all."

## Build phases

Kept small and reviewable, same pattern as every round so far — nothing here ships as one giant PR.

1. **Schema + data foundation.** `account_roles`, `segment_override`, `parent_account_id`,
   `reports_to_id`, `contact_class` — additive migrations only, plus the ingest-time default-class
   rule. No UI beyond what's needed to verify it in the DB/API.
2. **Customers-branch scaling.** Dashboard default filtering/sorting by `contact_class`/`allocated`
   for the Customers branch; role-based tab membership (an account can now appear in >1 branch).
3. **Analyze/BI view.** Aggregate panels + drilldown into a filtered contact list.
4. **Table view.** Configurable columns, CSV export; the Internal org-chart rendering.
5. **Suppliers go live** once real supplier data exists to ingest — schema's already there from
   phase 1, this is just "point ingest at the sheet."

## Explicit non-goals (for now)

- No multi-user accounts, permissions, or server deployment — still one person, one local SQLite
  file.
- No AI/LLM-based contact classification — rule + manual override, same philosophy as the existing
  department classifier.
- No automatic org-chart derivation — reporting lines are manually entered until a real source
  exists.
- No change to the Zoho write-safety model (`live_enabled` + typed confirmation) — the sync gets a
  smarter default selection, not a new permission to write more.

## Open questions to confirm before phase 1 starts

1. **Account roles vs. a simpler tag:** is `account_roles` (a proper many-to-many table) the right
   weight, or would a simpler `accounts.extra_roles` (JSON list, like the existing `extra` column
   pattern) be enough for how often an account actually spans customer+supplier in practice?
2. **Classification trigger:** is "allocated + resolved role → engaged by default" the right
   starting rule, or should *nothing* auto-classify and every contact start unclassified until a
   human (or a bulk action in the dashboard) sets it? Given the "no unverified contacts" principle,
   erring toward nothing auto-classifying may be closer to what you actually want.
3. **Org chart data**: besides manual entry, is there any existing source (even an informal one —
   an org chart slide, an HR list) worth a one-time import instead of starting from a blank slate?
