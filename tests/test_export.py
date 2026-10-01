"""Tests for the v4 Quote template export (V4-SPEC.md §5)."""
from __future__ import annotations

import openpyxl

from src import db, export


def test_quote_rows_shape_and_ref_prefix_stripped(cfg, loaded):
    anna = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")
    rows = export.quote_rows(loaded, [anna["id"]])
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == set(export.QUOTE_COLUMNS)
    assert row["Full Name"] == "Anna Smith"
    assert row["Company name"] == "Acme Security (Pty) Ltd"
    assert row["Email Address"] == "anna@acme.co.za"
    # source_id from the relational loader isn't the flat sheet's REF:-prefixed form,
    # so Ref is whatever's actually stored, unprefixed either way
    assert not (row["Ref"] or "").startswith("REF:")


def test_quote_rows_pulls_account_extra_fields(cfg, loaded):
    acme = db.one(loaded, "SELECT id FROM accounts WHERE name='Acme Security (Pty) Ltd'")
    anna = db.one(loaded, "SELECT id FROM contacts WHERE full_name='Anna Smith'")
    assert anna
    row = export.quote_rows(loaded, [anna["id"]])[0]
    assert row["Axis Partner"] == "Silver"
    assert row["Installer Category"] == "SubD"
    assert row["ACCNO"] == "ACM01"


def test_quote_rows_strips_ref_prefix_from_flat_ingest(cfg, conn, tmp_path):
    import openpyxl as oxl
    from src import ingest

    p = tmp_path / "flat.xlsx"
    wb = oxl.Workbook()
    ws = wb.active
    ws.append(["Ref", "Full Name", "Role", "Company name", "Email Address"])
    ws.append(["Zoe1", "Zoe Adams", "MD", "Zeta (Pty) Ltd", "zoe@zeta.com"])
    wb.save(p)
    cfg["inputs"]["workbook"] = str(p)
    cfg["inputs"]["sheet"] = None
    ingest.run(cfg, file=str(p))
    zoe = db.one(conn, "SELECT id FROM contacts WHERE full_name='Zoe Adams'")
    row = export.quote_rows(conn, [zoe["id"]])[0]
    assert row["Ref"] == "Zoe1"


def test_quote_rows_ignores_unknown_ids_and_empty_list(cfg, loaded):
    assert export.quote_rows(loaded, []) == []
    assert export.quote_rows(loaded, [999999]) == []


def test_quote_rows_orders_by_priority_then_name(cfg, loaded):
    ids = [r["id"] for r in db.rows(loaded, "SELECT id FROM contacts WHERE segment='customer'")]
    rows = export.quote_rows(loaded, ids)
    names = [r["Full Name"] for r in rows]
    priorities = db.rows(loaded, "SELECT full_name, priority FROM contacts WHERE segment='customer' "
                                 "ORDER BY priority IS NULL, priority, full_name")
    assert names == [r["full_name"] for r in priorities]


def test_rows_to_xlsx_round_trips(tmp_path):
    rows = [{"Full Name": "A", "Ref": "r1"}, {"Full Name": "B", "Ref": "r2"}]
    content = export.rows_to_xlsx(rows, columns=("Ref", "Full Name"))
    p = tmp_path / "out.xlsx"
    p.write_bytes(content)
    wb = openpyxl.load_workbook(p)
    ws = wb.active
    data = list(ws.iter_rows(values_only=True))
    assert data == [("Ref", "Full Name"), ("r1", "A"), ("r2", "B")]
