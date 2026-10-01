"""v4 phase: Zoho activity half of the engaged-classification trigger (V4-SPEC.md §2).

Reads an offline Zoho Contacts export (Settings -> Export, xlsx) ahead of a live API pull — it
carries 'Last Activity Time', which a live pull of standard fields doesn't currently request.
Matches rows to existing contacts (by email, then account + name, then name alone when unique)
and records the latest 'Last Activity Time' seen. `classify_from_activity` then fills `engaged`
for any still-unclassified contact with activity inside the configured window — never touching a
contact someone (or Qreg, once that's wired up) already classified.

    python -m src.zoho_activity import path/to/export.xlsx
    python -m src.zoho_activity classify [--days 365]

Note: this is the Zoho half only. Qreg (each rep/AM's own quote register) is the other half of
the spec's trigger and isn't wired up yet — no raw per-quote feed has been supplied, as opposed
to the Consolidated Sales Contacts sheet's AM/Category columns, which are a pre-digested roster,
not an activity log.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import openpyxl

from . import db
from .config import load_config
from .util import clean, norm_company, norm_name

DEFAULT_ENGAGED_ACTIVITY_DAYS = 365


def _read_export(path: str | Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [clean(h) for h in next(rows)]
    return [dict(zip(header, r)) for r in rows if any(v not in (None, "") for v in r)]


def _activity_str(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return clean(value)


def import_export(conn, path: str | Path) -> dict:
    """Match the export's rows to existing contacts and record the latest Last Activity Time
    seen. Never regresses a newer value already stored to an older one in the same file (or
    across repeated imports); fills a blank zoho_contact_id too, since this is ground truth."""
    records = _read_export(path)
    contacts = db.rows(conn, "SELECT id, name_norm, email, account_id, zoho_contact_id, zoho_last_activity "
                             "FROM contacts")
    accounts = {a["id"]: a["name_norm"] for a in db.rows(conn, "SELECT id, name_norm FROM accounts")}
    by_email: dict[str, dict] = {}
    by_name_acct: dict[tuple, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for c in contacts:
        if c["email"]:
            by_email.setdefault(c["email"].strip().lower(), c)
        by_name.setdefault(c["name_norm"], []).append(c)
        if c["account_id"]:
            by_name_acct.setdefault((c["name_norm"], accounts.get(c["account_id"])), []).append(c)

    matched = updated = unmatched = 0
    with conn:
        for rec in records:
            full_name = clean(rec.get("Contact Name")) or " ".join(
                filter(None, [clean(rec.get("First Name")), clean(rec.get("Last Name"))]))
            if not full_name:
                continue
            email = clean(rec.get("Email"))
            contact = by_email.get(email.lower()) if email else None
            if not contact:
                acct = clean(rec.get("Account Name"))
                cands = by_name_acct.get((norm_name(full_name), norm_company(acct))) if acct else None
                contact = cands[0] if cands else None
            if not contact:
                cands = by_name.get(norm_name(full_name))
                contact = cands[0] if cands and len(cands) == 1 else None
            if not contact:
                unmatched += 1
                continue
            matched += 1
            activity = _activity_str(rec.get("Last Activity Time")) or _activity_str(rec.get("Modified Time"))
            changes = {}
            if activity and (not contact["zoho_last_activity"] or activity > contact["zoho_last_activity"]):
                changes["zoho_last_activity"] = activity
            zoho_id = clean(rec.get("Record Id"))
            if zoho_id and not contact["zoho_contact_id"]:
                changes["zoho_contact_id"] = zoho_id
            if changes:
                db.update(conn, "contacts", contact["id"], changes, touch=False)
                updated += 1
    return {"rows": len(records), "matched": matched, "updated": updated, "unmatched": unmatched}


def classify_from_activity(conn, days: int = DEFAULT_ENGAGED_ACTIVITY_DAYS) -> dict:
    """Fill-blank-only: an unclassified contact with Zoho activity inside the window becomes
    `engaged`. Never touches a contact that already has a contact_class, manual or otherwise —
    same 'never overwrite a stronger source' philosophy as role_source/department_source."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cur = conn.execute(
            "UPDATE contacts SET contact_class = 'engaged' "
            "WHERE contact_class IS NULL AND zoho_last_activity IS NOT NULL AND zoho_last_activity >= ?",
            [cutoff])
    return {"classified_engaged": cur.rowcount, "cutoff": cutoff}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p_import = sub.add_parser("import", help="read a Zoho Contacts export (xlsx)")
    p_import.add_argument("path")
    p_classify = sub.add_parser("classify", help="fill contact_class='engaged' from recorded activity")
    p_classify.add_argument("--days", type=int, default=None)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    try:
        if args.action == "import":
            result = import_export(conn, args.path)
        else:
            days = args.days if args.days is not None else (
                (cfg.get("classification") or {}).get("engaged_activity_days", DEFAULT_ENGAGED_ACTIVITY_DAYS))
            result = classify_from_activity(conn, days)
    finally:
        conn.close()
    print(result)


if __name__ == "__main__":
    main()
