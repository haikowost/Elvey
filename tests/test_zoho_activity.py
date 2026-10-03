"""Tests for the offline Zoho Contacts export import (v4 §2 Zoho-activity half)."""
from __future__ import annotations

import openpyxl

from src import db, zoho_activity


def _write_export(path, rows):
    headers = ["Record Id", "First Name", "Last Name", "Contact Name", "Account Name", "Email",
               "Last Activity Time", "Modified Time"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h) for h in headers])
    wb.save(path)
    return path


def test_import_matches_by_email_and_records_latest(cfg, loaded, tmp_path):
    anna = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")
    p = _write_export(tmp_path / "export1.xlsx", [
        {"Record Id": "z1", "Contact Name": "Anna Smith", "Account Name": "Acme Security (Pty) Ltd",
         "Email": "ANNA@acme.co.za", "Last Activity Time": "2026-01-15 10:00:00"},
    ])
    out = zoho_activity.import_export(loaded, p)
    assert out == {"rows": 1, "matched": 1, "updated": 1, "unmatched": 0}
    row = db.one(loaded, "SELECT zoho_last_activity, zoho_contact_id FROM contacts WHERE id=?", [anna["id"]])
    assert row["zoho_last_activity"] == "2026-01-15 10:00:00" and row["zoho_contact_id"] == "z1"

    # a second, earlier-dated import for the same person never regresses the stored activity
    p2 = _write_export(tmp_path / "export2.xlsx", [
        {"Record Id": "z1", "Contact Name": "Anna Smith", "Account Name": "Acme Security (Pty) Ltd",
         "Email": "anna@acme.co.za", "Last Activity Time": "2025-06-01 09:00:00"},
    ])
    zoho_activity.import_export(loaded, p2)
    row2 = db.one(loaded, "SELECT zoho_last_activity FROM contacts WHERE id=?", [anna["id"]])
    assert row2["zoho_last_activity"] == "2026-01-15 10:00:00"


def test_import_matches_by_account_and_name_without_email(cfg, loaded, tmp_path):
    carla = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Carla Müller'")
    p = _write_export(tmp_path / "export.xlsx", [
        {"Record Id": "z2", "Contact Name": "Carla Müller", "Account Name": "Beta Integrators",
         "Email": "", "Last Activity Time": "2026-02-01 08:00:00"},
    ])
    out = zoho_activity.import_export(loaded, p)
    assert out["matched"] == 1 and out["updated"] == 1
    row = db.one(loaded, "SELECT zoho_last_activity FROM contacts WHERE id=?", [carla["id"]])
    assert row["zoho_last_activity"] == "2026-02-01 08:00:00"


def test_import_leaves_unmatched_rows_alone(cfg, loaded, tmp_path):
    p = _write_export(tmp_path / "export.xlsx", [
        {"Record Id": "z3", "Contact Name": "Nobody Here", "Account Name": "Somewhere Else",
         "Email": "nobody@nowhere.com", "Last Activity Time": "2026-02-01 08:00:00"},
    ])
    out = zoho_activity.import_export(loaded, p)
    assert out == {"rows": 1, "matched": 0, "updated": 0, "unmatched": 1}


def test_classify_from_activity_fills_blank_only_within_window(cfg, loaded):
    anna = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")
    bob = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Bob Jones'")
    carla = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Carla Müller'")
    loaded.execute("UPDATE contacts SET zoho_last_activity='2026-09-01 00:00:00' WHERE id=?", [anna["id"]])
    loaded.execute("UPDATE contacts SET zoho_last_activity='2020-01-01 00:00:00' WHERE id=?", [bob["id"]])  # too old
    loaded.execute("UPDATE contacts SET zoho_last_activity='2026-09-01 00:00:00', contact_class='backlog' "
                   "WHERE id=?", [carla["id"]])  # already classified -> never touched
    loaded.commit()

    out = zoho_activity.classify_from_activity(loaded, days=365)
    assert out["classified_engaged"] == 1
    assert db.one(loaded, "SELECT contact_class FROM contacts WHERE id=?", [anna["id"]])["contact_class"] == "engaged"
    assert db.one(loaded, "SELECT contact_class FROM contacts WHERE id=?", [bob["id"]])["contact_class"] is None
    assert db.one(loaded, "SELECT contact_class FROM contacts WHERE id=?", [carla["id"]])["contact_class"] == "backlog"
