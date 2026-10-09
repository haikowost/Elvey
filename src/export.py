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

import argparse
import io
import json
import shutil
import time
from pathlib import Path

import openpyxl

from . import db
from .config import load_config

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


# --------------------------------------------------------------------------- enriched round-trip

ENRICHED_EXTRA = ("LinkedIn Profile", "LinkedIn Summary", "Department", "Employment", "Current Company (LinkedIn)",
                  "City", "Province", "Country", "Photo File", "KYC Status", "Enriched At")


def enriched_rows(conn) -> list[dict]:
    """Every active contact in the Consolidated Sales Contacts column layout (so it re-imports
    cleanly), plus what the LinkedIn harvest added."""
    ids = [r["id"] for r in db.rows(conn, "SELECT id FROM contacts WHERE contact_status = 'active'")]
    base = quote_rows(conn, ids)
    extra = {r["full_name"] + "|" + (r["email"] or ""): r for r in db.rows(conn, """
        SELECT full_name, email, linkedin_profile_url, linkedin_summary, department, employment_status,
               linkedin_current_company, city, province, country, image_filename, image_status,
               enrich_status, enrich_last_at FROM contacts WHERE contact_status = 'active'""")}
    for row in base:
        e = extra.get(f"{row['Full Name']}|{row['Email Address'] or ''}") or {}
        has_photo = e.get("image_status") in ("downloaded", "manual")
        row.update({"LinkedIn Profile": e.get("linkedin_profile_url"), "LinkedIn Summary": e.get("linkedin_summary"),
                    "Department": e.get("department"), "Employment": e.get("employment_status"),
                    "Current Company (LinkedIn)": e.get("linkedin_current_company"), "City": e.get("city"),
                    "Province": e.get("province"), "Country": e.get("country"),
                    "Photo File": e.get("image_filename") if has_photo else None,
                    "KYC Status": e.get("enrich_status"), "Enriched At": e.get("enrich_last_at")})
        if row.get("Role") is None:
            row["Role"] = "lookup pending (KYC app)"  # keep the sheet's own placeholder on round-trip
    return base


def export_enriched(conn, cfg, out_dir: Path | None = None) -> dict:
    """data/exports/Elvey KYC enriched contacts <date>.xlsx + data/exports/faces/<face files>."""
    out_dir = Path(out_dir or cfg.data_dir / "exports")
    faces_out = out_dir / "faces"
    faces_out.mkdir(parents=True, exist_ok=True)
    rows = enriched_rows(conn)
    xlsx = out_dir / f"Elvey KYC enriched contacts {time.strftime('%Y%m%d')}.xlsx"
    xlsx.write_bytes(rows_to_xlsx(rows, QUOTE_COLUMNS + ENRICHED_EXTRA))
    copied = missing = 0
    src = cfg.kyc_folder
    for r in rows:
        name = r.get("Photo File")
        if not name:
            continue
        f = (src / name) if src else None
        if f and f.is_file():
            target = faces_out / name
            if not target.exists() or target.stat().st_mtime < f.stat().st_mtime:
                shutil.copy2(f, target)
            copied += 1
        else:
            missing += 1
    return {"xlsx": str(xlsx), "contacts": len(rows), "faces_dir": str(faces_out), "faces": copied,
            "faces_missing": missing}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Export the enriched contacts (.xlsx) + face images")
    ap.add_argument("--out", help="output folder (default data/exports)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    print(json.dumps(export_enriched(db.connect(cfg.db_path), cfg, args.out), indent=2))


if __name__ == "__main__":
    main()
