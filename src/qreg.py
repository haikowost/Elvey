"""v4 phase: the Qreg half of the engaged-classification trigger (V4-SPEC.md §2).

Each rep/account manager keeps their own copy of the Pentagon Quotation Template workbook
(MASTERv12 and its revisions), which carries a 'QReg' sheet — one row per quote raised, with the
quote date and the customer's company/contact/email. This reads one or more of those workbooks
and records each matched contact's latest quote date (`contacts.qreg_last_quote`), the same shape
as `zoho_activity.py`'s Last Activity Time. Run both and whichever fires first wins, fill-blank-
only — that's the spec's "engaged from Qreg OR Zoho activity" without needing a merged function.

The sheet's row-2 header repeats the literal text 'Customer' for the name/email/phone sub-columns
(a merged-cell artifact of the template), so those three are read by fixed position rather than
alias-matched like the flat ingest sheet — this is a single shared template, not a varying export.

    python -m src.qreg import RepName_Pentagon_Quotation_Template....xlsm [more.xlsm ...]
    python -m src.qreg classify [--days 365]
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import openpyxl

from . import db
from .config import load_config
from .util import clean, norm_company, norm_name

DEFAULT_ENGAGED_QUOTE_DAYS = 365

# Fixed QReg column positions (0-indexed), consistent across every rep's copy of the template.
COL_QUOTE_DATE, COL_COMPANY, COL_CONTACT_NAME, COL_EMAIL = 1, 6, 7, 8
HEADER_ROWS = 2
_AUTH_SENTINEL = "auth"


def _read_qreg(path: str | Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_vba=False)
    ws = wb["QReg"]
    rows = ws.iter_rows(values_only=True)
    for _ in range(HEADER_ROWS):
        next(rows, None)
    out = []
    for r in rows:
        company = clean(r[COL_COMPANY]) if len(r) > COL_COMPANY else None
        quote_date = r[COL_QUOTE_DATE] if len(r) > COL_QUOTE_DATE else None
        if not company or not quote_date or company.lower() == _AUTH_SENTINEL:
            continue
        out.append({
            "quote_date": quote_date, "company": company,
            "contact_name": clean(r[COL_CONTACT_NAME]) if len(r) > COL_CONTACT_NAME else None,
            "email": clean(r[COL_EMAIL]) if len(r) > COL_EMAIL else None,
        })
    return out


def _date_str(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return clean(value)


def import_quote_register(conn, path: str | Path) -> dict:
    """Match each QReg row to an existing contact — by email, then by (account, contact name),
    then by account alone when it has exactly one contact — and record the latest quote date
    seen. Never regresses an already-recorded later date (a rep's workbook re-exported later
    repeats every prior quote)."""
    rows = _read_qreg(path)
    contacts = db.rows(conn, "SELECT id, name_norm, email, account_id, qreg_last_quote FROM contacts")
    accounts = {a["id"]: a["name_norm"] for a in db.rows(conn, "SELECT id, name_norm FROM accounts")}
    by_email: dict[str, dict] = {}
    by_acct_name: dict[tuple, list[dict]] = {}
    by_acct: dict[str, list[dict]] = {}
    for c in contacts:
        if c["email"]:
            by_email.setdefault(c["email"].strip().lower(), c)
        if c["account_id"]:
            acct_key = accounts.get(c["account_id"])
            by_acct_name.setdefault((acct_key, c["name_norm"]), []).append(c)
            by_acct.setdefault(acct_key, []).append(c)

    matched = updated = unmatched = 0
    with conn:
        for r in rows:
            contact = by_email.get(r["email"].strip().lower()) if r["email"] else None
            if not contact and r["contact_name"]:
                cands = by_acct_name.get((norm_company(r["company"]), norm_name(r["contact_name"])))
                contact = cands[0] if cands else None
            if not contact:
                cands = by_acct.get(norm_company(r["company"]))
                contact = cands[0] if cands and len(cands) == 1 else None
            if not contact:
                unmatched += 1
                continue
            matched += 1
            quote_date = _date_str(r["quote_date"])
            if quote_date and (not contact["qreg_last_quote"] or quote_date > contact["qreg_last_quote"]):
                db.update(conn, "contacts", contact["id"], {"qreg_last_quote": quote_date}, touch=False)
                updated += 1
    return {"rows": len(rows), "matched": matched, "updated": updated, "unmatched": unmatched}


def classify_from_quotes(conn, days: int = DEFAULT_ENGAGED_QUOTE_DAYS) -> dict:
    """Fill-blank-only: an unclassified contact with a quote inside the window becomes `engaged`.
    Never touches a contact that already has a contact_class, manual or from Zoho activity."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with conn:
        cur = conn.execute(
            "UPDATE contacts SET contact_class = 'engaged' "
            "WHERE contact_class IS NULL AND qreg_last_quote IS NOT NULL AND qreg_last_quote >= ?",
            [cutoff])
    return {"classified_engaged": cur.rowcount, "cutoff": cutoff}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p_import = sub.add_parser("import", help="read one or more reps' QReg sheets")
    p_import.add_argument("paths", nargs="+")
    p_classify = sub.add_parser("classify", help="fill contact_class='engaged' from recorded quotes")
    p_classify.add_argument("--days", type=int, default=None)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    try:
        if args.action == "import":
            result = [{"path": p, **import_quote_register(conn, p)} for p in args.paths]
        else:
            days = args.days if args.days is not None else (
                (cfg.get("classification") or {}).get("engaged_quote_days", DEFAULT_ENGAGED_QUOTE_DAYS))
            result = classify_from_quotes(conn, days)
    finally:
        conn.close()
    print(result)


if __name__ == "__main__":
    main()
