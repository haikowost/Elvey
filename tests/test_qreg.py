"""Tests for the rep QReg-sheet import (v4 §2 Qreg half of the engaged trigger)."""
from __future__ import annotations

import openpyxl

from src import db, qreg


def _write_qreg(path, rows):
    """Builds a minimal workbook shaped like a rep's Pentagon Quotation Template: a 'QReg' sheet
    with two header rows (the real template merges cells across row 1; row 2 repeats 'Customer'
    for the name/email/phone sub-columns, which is exactly why qreg.py reads by fixed position)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "QReg"
    ws.append(["Entry"] + [None] * 5 + ["Customer"] + [None] * 4 + ["Forecast"])
    ws.append(["Q No", "Quote Date", "For", "Ref", "PID", None, "Company ", "Customer", "Customer", "Customer",
               None, "Value"])
    for r in rows:
        row = [None] * 12
        row[1] = r.get("quote_date")
        row[6] = r.get("company")
        row[7] = r.get("contact_name")
        row[8] = r.get("email")
        ws.append(row)
    wb.save(path)
    return path


def test_import_matches_by_email_and_records_latest(cfg, loaded, tmp_path):
    anna = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")
    p = _write_qreg(tmp_path / "rep1.xlsm", [
        {"quote_date": "2026-01-15", "company": "Acme Security (Pty) Ltd", "contact_name": "Anna Smith",
         "email": "ANNA@acme.co.za"},
    ])
    out = qreg.import_quote_register(loaded, p)
    assert out == {"rows": 1, "matched": 1, "updated": 1, "unmatched": 0}
    row = db.one(loaded, "SELECT qreg_last_quote FROM contacts WHERE id=?", [anna["id"]])
    assert row["qreg_last_quote"] == "2026-01-15"

    # a later re-export with an older quote in it never regresses the stored date
    p2 = _write_qreg(tmp_path / "rep1b.xlsm", [
        {"quote_date": "2025-06-01", "company": "Acme Security (Pty) Ltd", "contact_name": "Anna Smith",
         "email": "anna@acme.co.za"},
    ])
    qreg.import_quote_register(loaded, p2)
    assert db.one(loaded, "SELECT qreg_last_quote FROM contacts WHERE id=?", [anna["id"]])["qreg_last_quote"] == "2026-01-15"


def test_import_matches_by_account_and_contact_name_without_email(cfg, loaded, tmp_path):
    carla = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Carla Müller'")
    p = _write_qreg(tmp_path / "rep.xlsm", [
        {"quote_date": "2026-02-01", "company": "Beta Integrators", "contact_name": "Carla Müller", "email": None},
    ])
    out = qreg.import_quote_register(loaded, p)
    assert out["matched"] == 1 and out["updated"] == 1
    assert db.one(loaded, "SELECT qreg_last_quote FROM contacts WHERE id=?", [carla["id"]])["qreg_last_quote"] == "2026-02-01"


def test_import_matches_by_account_alone_when_unique_contact(cfg, loaded, tmp_path):
    carla = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Carla Müller'")
    p = _write_qreg(tmp_path / "rep.xlsm", [
        {"quote_date": "2026-03-01", "company": "Beta Integrators", "contact_name": None, "email": None},
    ])
    out = qreg.import_quote_register(loaded, p)
    assert out["matched"] == 1
    assert db.one(loaded, "SELECT qreg_last_quote FROM contacts WHERE id=?", [carla["id"]])["qreg_last_quote"] == "2026-03-01"


def test_import_skips_the_auth_sentinel_row_and_rows_without_a_date(cfg, loaded, tmp_path):
    p = _write_qreg(tmp_path / "rep.xlsm", [
        {"quote_date": None, "company": "Auth", "contact_name": "Auth", "email": None},
        {"quote_date": None, "company": "Acme Security (Pty) Ltd", "contact_name": "Anna Smith", "email": None},
    ])
    out = qreg.import_quote_register(loaded, p)
    assert out == {"rows": 0, "matched": 0, "updated": 0, "unmatched": 0}


def test_import_leaves_unmatched_rows_alone(cfg, loaded, tmp_path):
    p = _write_qreg(tmp_path / "rep.xlsm", [
        {"quote_date": "2026-02-01", "company": "Somewhere Else", "contact_name": "Nobody Here", "email": None},
    ])
    out = qreg.import_quote_register(loaded, p)
    assert out == {"rows": 1, "matched": 0, "updated": 0, "unmatched": 1}


def test_classify_from_quotes_fills_blank_only_within_window(cfg, loaded):
    anna = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")
    bob = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Bob Jones'")
    carla = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Carla Müller'")
    loaded.execute("UPDATE contacts SET qreg_last_quote='2026-09-01' WHERE id=?", [anna["id"]])
    loaded.execute("UPDATE contacts SET qreg_last_quote='2020-01-01' WHERE id=?", [bob["id"]])  # too old
    loaded.execute("UPDATE contacts SET qreg_last_quote='2026-09-01', contact_class='lead' WHERE id=?", [carla["id"]])
    loaded.commit()

    out = qreg.classify_from_quotes(loaded, days=365)
    assert out["classified_engaged"] == 1
    assert db.one(loaded, "SELECT contact_class FROM contacts WHERE id=?", [anna["id"]])["contact_class"] == "engaged"
    assert db.one(loaded, "SELECT contact_class FROM contacts WHERE id=?", [bob["id"]])["contact_class"] is None
    assert db.one(loaded, "SELECT contact_class FROM contacts WHERE id=?", [carla["id"]])["contact_class"] == "lead"
