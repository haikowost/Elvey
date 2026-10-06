"""v4 phase 5: exports (V4-SPEC.md §5).

The generic filtered CSV export (any column set, from whatever's currently shown in the People
Tree or Table view) is client-side JavaScript — `exportTableCsv()` in `src/static/index.html` —
since the browser already holds exactly the filtered/sorted rows the user is looking at.

This module is the other half: the **Quote template export**, a *named preset* of the same idea
that needs server-side support because its column set — the Consolidated Sales Contacts sheet's
own layout — pulls fields `/api/people` doesn't carry at all (ACCNO, Cluster, Axis Partner,
Milestone Partner, Installer Category, Sort). It's a round-trip: the same shape `src/ingest.py`
reads, written back out from the current DB state for whatever contacts are currently
filtered/selected in the dashboard, as an .xlsx (the sheet's native format — "where formatting
matters", per the spec).
"""
from __future__ import annotations

import io

import openpyxl

from . import db

# Exact column order of the Consolidated Sales Contacts sheet (src/ingest.py's FLAT_ALIASES).
QUOTE_COLUMNS = ("Ref", "Full Name", "Role", "Region/Branch", "Office Tel", "Cell number",
                 "Email Address", "Linkedin", "Company name", "ACCNO", "Axis Partner",
                 "Milestone Partner", "Installer Category", "AM", "Category", "In Zoho",
                 "In MakDB", "Cluster", "Sort")


def quote_rows(conn, contact_ids: list[int]) -> list[dict]:
    """One row per contact id, in QUOTE_COLUMNS shape, straight from the current DB state —
    including anything the harvester or a manual edit has since filled in. Unknown ids are
    skipped rather than erroring, since the id list comes from a client-side filtered view that
    could be stale by a request round-trip."""
    if not contact_ids:
        return []
    placeholders = ",".join("?" for _ in contact_ids)
    rows = db.rows(conn, f"""
        SELECT c.source_id, c.full_name, c.role, a.branch, c.tel, c.cell, c.email,
               COALESCE(c.linkedin_contact_url, c.linkedin_profile_url) AS linkedin,
               a.name AS company, a.accno, a.extra, c.allocated_rep, c.category, c.in_zoho,
               c.in_makdb, a.cluster, c.priority
        FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
        WHERE c.id IN ({placeholders})
        ORDER BY c.priority IS NULL, c.priority, c.full_name""", contact_ids)
    out = []
    for r in rows:
        extra = db.jload(r["extra"], {})
        ref = r["source_id"]
        if ref and ref.startswith("REF:"):
            ref = ref[4:]
        out.append({
            "Ref": ref, "Full Name": r["full_name"], "Role": r["role"], "Region/Branch": r["branch"],
            "Office Tel": r["tel"], "Cell number": r["cell"], "Email Address": r["email"],
            "Linkedin": r["linkedin"], "Company name": r["company"], "ACCNO": r["accno"],
            "Axis Partner": extra.get("axis_partner"), "Milestone Partner": extra.get("milestone_partner"),
            "Installer Category": extra.get("installer_category"), "AM": r["allocated_rep"],
            "Category": r["category"], "In Zoho": r["in_zoho"], "In MakDB": r["in_makdb"],
            "Cluster": r["cluster"], "Sort": r["priority"],
        })
    return out


def rows_to_xlsx(rows: list[dict], columns: tuple[str, ...] = QUOTE_COLUMNS, sheet_name: str = "Customers (import)") -> bytes:
    """Writes `rows` (dicts keyed by `columns`) to an .xlsx, one sheet, header row + data."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(list(columns))
    for row in rows:
        ws.append([row.get(c) for c in columns])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
