import csv
import json

from src import db, harvest, images, ingest, orgchart
from src.util import (category_rank, face_filename, infer_supplier_from_title, is_subdistributor_category,
                      norm_company, parse_face_filename, parse_money, resolve_role, slug, split_name, yn)


# ------------------------------------------------------------------ util

def test_naming_convention(cfg):
    assert slug("Leandro da Cunha") == "Leandro_da_Cunha"
    assert slug("René Müller-Smith") == "Rene_Muller_Smith"
    assert face_filename("competitor", "Leandro da Cunha", "Duxbury", cfg) == "Duxbury__Leandro_da_Cunha.jpg"
    assert face_filename("customer", "Anton Bothma", "Fidelity", cfg) == "CUST__Anton_Bothma.jpg"
    assert face_filename("internal", "Robyn-Lee Grove", "Elvey", cfg) == "INT__Robyn_Lee_Grove.jpg"
    assert parse_face_filename("Reditron__Gordon_Moore.jpg") == ("Reditron", "Gordon_Moore")
    assert parse_face_filename("random.jpg") is None
    assert split_name("Leandro da Cunha") == ("Leandro", "da Cunha")


def test_resolve_role_and_yn():
    assert resolve_role("lookup pending (KYC app)") == (None, "pending")
    assert resolve_role("Lookup Pending") == (None, "pending")
    assert resolve_role(None) == (None, "pending")
    assert resolve_role("") == (None, "pending")
    assert resolve_role("Buyer") == ("Buyer", "data")
    assert yn("Y") == yn("yes") == yn("1") == "Y"
    assert yn("N") == yn("no") == yn("0") == "N"
    assert yn("Company only") == "Company only"
    assert yn(None) is None and yn("") is None


def test_category_rank_orders_by_cadence():
    assert category_rank("A") < category_rank("B") < category_rank("C") < category_rank("D")
    assert category_rank("a") == category_rank("A")  # case-insensitive
    assert category_rank(None) > category_rank("D")  # unknown/blank sorts last of all


def test_parse_money_and_company():
    assert parse_money("R 1,234,567.89") == 1234567.89
    assert parse_money("1.2m") == 1_200_000
    assert parse_money("(1 200)") == -1200
    assert parse_money(16341175.1) == 16341175.1
    assert parse_money("") is None and parse_money("n/a") is None
    assert norm_company("Acme Security (Pty) Ltd") == norm_company("ACME SECURITY") == "acme security"


# ------------------------------------------------------------------ ingest

def test_ingest_relational_counts_and_mapping(cfg, conn):
    out = ingest.run(cfg)
    s = out["db"]
    # top-2 accounts + accounts of in-scope contacts (+ Elvey/Duxbury/Reditron seeds)
    names = {r["name"]: r for r in db.rows(conn, "SELECT * FROM accounts")}
    assert {"Acme Security (Pty) Ltd", "Beta Integrators", "Elvey", "Duxbury", "Reditron"} <= set(names)
    assert "Gamma Systems" not in names  # rank 3 account, contact rank 9 > top 6
    acme = names["Acme Security (Pty) Ltd"]
    assert acme["rank"] == 1 and acme["rep"] == "Siresen Naidoo" and acme["latest_sellout"] == 2500000.5
    assert acme["brand_focus"] == "Milestone, Bosch"
    assert acme["allocation"].startswith("Conflict")
    assert json.loads(acme["extra"])["tasha_visit"] == "High"
    assert names["Elvey"]["segment"] == "internal" and names["Duxbury"]["segment"] == "competitor"
    assert names["Reditron"]["segment"] == "competitor"

    people = {r["full_name"]: r for r in db.rows(conn, "SELECT * FROM contacts")}
    assert s["contacts"] == len(people) == 6  # Bob deduped, Jaco merged, Dan out of scope
    bob = people["Bob Jones"]
    assert bob["role"] == "Buyer" and bob["cell"] == "+27 83 111 2222" and bob["priority"] == 2
    assert bob["linkedin_contact_url"] == "https://www.linkedin.com/in/bobjones"
    assert people["Anna Smith"]["email"] == "anna@acme.co.za" and people["Anna Smith"]["linkedin_contact_url"] is None
    jaco = people["Jaco Moolman"]
    assert jaco["segment"] == "internal" and jaco["role"] == "Director"
    assert people["Leandro da Cunha"]["segment"] == "competitor"


def test_ingest_idempotent_and_non_destructive(cfg, conn):
    ingest.run(cfg)
    conn.execute("UPDATE contacts SET linkedin_summary='kept', zoho_contact_id='Z1' WHERE full_name='Anna Smith'")
    conn.commit()
    out = ingest.run(cfg)
    assert set(out["upsert"]) <= {"accounts_unchanged", "contacts_unchanged"}
    anna = db.one(conn, "SELECT * FROM contacts WHERE full_name='Anna Smith'")
    assert anna["linkedin_summary"] == "kept" and anna["zoho_contact_id"] == "Z1"


def test_ingest_all_scope(cfg, conn):
    out = ingest.run(cfg, all_rows=True)
    assert out["db"]["contacts"] == 7  # Dan Out now included
    assert db.one(conn, "SELECT count(*) n FROM accounts WHERE name='Gamma Systems'")["n"] == 1


def test_ingest_flat_csv(cfg, conn, tmp_path):
    p = tmp_path / "flat.csv"
    p.write_text("Company,Contact Name,Job Title,E-mail,Mobile,Rank,FY26 Sellout,Rep,Division\n"
                 "Zeta (Pty) Ltd,Zoe Adams,MD,ZOE@zeta.com,082 1,1,\"R 1,000\",SN,Elvey Projects\n"
                 "Zeta (Pty) Ltd,Yan Li,Tech,,,2,,SN,\n", encoding="utf-8")
    out = ingest.run(cfg, file=str(p))
    assert out["loaded"] == {"accounts": 1, "contacts": 2}
    z = db.one(conn, "SELECT * FROM accounts WHERE name_norm='zeta'")
    assert z["latest_sellout"] == 1000 and z["division"] == "Elvey Projects"
    assert db.one(conn, "SELECT email FROM contacts WHERE full_name='Zoe Adams'")["email"] == "zoe@zeta.com"


def test_ingest_v3_cleaned_sales_contacts_sheet(cfg, conn, tmp_path):
    """The v3 source: 'Ref, Full Name, Role, Region/Branch, ..., AM, Category, In Zoho, In MakDB'.
    A real Role is kept and protected; the 'lookup pending (KYC app)' placeholder is not."""
    p = tmp_path / "cleaned.csv"
    p.write_text(
        "Ref,Full Name,Role,Region/Branch,Cell number,Email Address,Company name,AM,Category,In Zoho,In MakDB,Sort\n"
        "R001,Zoe Adams,Branch Manager,Gauteng,082 111 2222,zoe@zeta.com,Zeta (Pty) Ltd,SN,A,Y,Y,1\n"
        "R002,Yan Li,lookup pending (KYC app),Gauteng,,,Zeta (Pty) Ltd,Unallocated,D,N,N,2\n",
        encoding="utf-8")
    ingest.run(cfg, file=str(p))
    zoe = db.one(conn, "SELECT * FROM contacts WHERE full_name='Zoe Adams'")
    assert zoe["role"] == "Branch Manager" and zoe["role_source"] == "data"
    assert zoe["allocated_rep"] == "SN" and zoe["allocated"] == 1 and zoe["category"] == "A"
    assert zoe["in_zoho"] == "Y" and zoe["in_makdb"] == "Y" and zoe["cell"] == "+27 82 111 2222"
    assert zoe["source_id"] == "REF:R001"
    yan = db.one(conn, "SELECT * FROM contacts WHERE full_name='Yan Li'")
    assert yan["role"] is None and yan["role_source"] == "pending"  # placeholder text never stored as the role
    assert yan["allocated_rep"] == "Unallocated" and yan["allocated"] == 0
    assert yan["in_zoho"] == "N" and yan["in_makdb"] == "N"


def test_category_is_the_priority_fallback_not_row_order(cfg, conn, tmp_path):
    """Reported symptom: without an explicit Sort value, everyone's priority used to be their
    row position in the sheet, so whoever happened to be listed first always showed up first in
    the People Tree / Corrections chat - regardless of how urgent they actually are. The fallback
    is now the Category cadence (A before D), with name as the tiebreaker within a category."""
    p = tmp_path / "flat.csv"
    p.write_text(
        "Company,Contact Name,Category\n"
        "Zeta (Pty) Ltd,Vashna First,D\n"   # listed first in the sheet, but low-priority category
        "Zeta (Pty) Ltd,Amara Weekly,A\n"   # listed last, but the highest call-cadence priority
        "Zeta (Pty) Ltd,Zane Weekly,A\n",
        encoding="utf-8")
    ingest.run(cfg, file=str(p))
    vashna = db.one(conn, "SELECT * FROM contacts WHERE full_name='Vashna First'")
    amara, zane = (db.one(conn, "SELECT * FROM contacts WHERE full_name=?", [n]) for n in ("Amara Weekly", "Zane Weekly"))
    assert amara["priority"] == zane["priority"] == 1 and vashna["priority"] == 4  # A=1, D=4
    assert amara["priority"] < vashna["priority"]  # category A now outranks row order entirely
    # same category -> tied priority -> dashboard breaks the tie by name (Amara before Zane)


def test_flat_csv_location_beats_linkedin_guess(cfg, conn, tmp_path):
    """A location known from the source data (a hypothetical future column) must never be
    silently overwritten by a LinkedIn guess."""
    p = tmp_path / "flat.csv"
    p.write_text("Company,Contact Name,City,Province,Country,Rank\n"
                 "Zeta (Pty) Ltd,Zoe Adams,Cape Town,Western Cape,South Africa,1\n", encoding="utf-8")
    ingest.run(cfg, file=str(p))
    zoe = db.one(conn, "SELECT * FROM contacts WHERE full_name='Zoe Adams'")
    assert (zoe["city"], zoe["province"], zoe["country"], zoe["location_source"]) == \
        ("Cape Town", "Western Cape", "South Africa", "database")
    res = harvest.Result("done", profile_url="u", profile_name="Zoe Adams", location="Somewhere Else, Elsewhere")
    harvest.apply_result(conn, cfg, zoe, res, need_face=False)
    kept = db.one(conn, "SELECT city, location_source FROM contacts WHERE id=?", [zoe["id"]])
    assert kept == {"city": "Cape Town", "location_source": "database"}  # unchanged


def test_harvest_fills_a_pending_role_but_never_a_spreadsheet_one(cfg, conn, tmp_path):
    """The v3 fix: a sheet role of 'lookup pending (KYC app)' (role_source='pending') gets filled
    from LinkedIn; a real spreadsheet role (role_source='data') is still never touched."""
    p = tmp_path / "flat.csv"
    p.write_text("Company,Contact Name,Role,Rank\n"
                 "Zeta (Pty) Ltd,Zoe Adams,lookup pending (KYC app),1\n"
                 "Zeta (Pty) Ltd,Yan Li,Technician,2\n", encoding="utf-8")
    ingest.run(cfg, file=str(p))
    zoe, yan = (db.one(conn, "SELECT * FROM contacts WHERE full_name=?", [n]) for n in ("Zoe Adams", "Yan Li"))
    assert zoe["role"] is None and zoe["role_source"] == "pending"

    for c, headline in ((zoe, "Branch Manager at Zeta"), (yan, "Senior Technician at Zeta")):
        res = harvest.Result("done", profile_url="u", profile_name=c["full_name"], headline=headline, about="x")
        harvest.apply_result(conn, cfg, c, res, need_face=False)
    zoe2, yan2 = (db.one(conn, "SELECT * FROM contacts WHERE id=?", [c["id"]]) for c in (zoe, yan))
    assert zoe2["role"] == "Branch Manager" and zoe2["role_source"] == "linkedin"  # pending -> filled
    assert yan2["role"] == "Technician" and yan2["role_source"] == "data"          # spreadsheet role kept


# ------------------------------------------------------------------ images

def test_images_match_every_face(cfg, conn):
    ingest.run(cfg)
    out = images.match(conn, cfg)
    # Tasha has no contact row, Carla matches via accent-free slug (Müller -> Muller)
    assert out["unmatched_files"] == ["INT__Tasha_Smith.jpg"]
    carla = db.one(conn, "SELECT * FROM contacts WHERE full_name='Carla Müller'")
    assert carla["image_filename"] == "CUST__Carla_Muller.jpg" and carla["image_status"] == "downloaded"
    out = images.match(conn, cfg, create_missing=True)
    assert out["unmatched_files"] == [] and out["contacts_created"] == 1
    tasha = db.one(conn, "SELECT c.*, a.name company FROM contacts c JOIN accounts a ON a.id=c.account_id WHERE full_name='Tasha Smith'")
    assert tasha["segment"] == "internal" and tasha["company"] == "Elvey"
    assert images.match(conn, cfg)["contacts_updated"] == 0  # idempotent


def test_images_missing_file_resets_and_manual_kept(cfg, conn):
    ingest.run(cfg)
    images.match(conn, cfg)
    conn.execute("UPDATE contacts SET image_status='manual' WHERE full_name='Gordon Moore'")
    conn.commit()
    (cfg.kyc_folder / "CUST__Anna_Smith.jpg").unlink()
    (cfg.kyc_folder / "Reditron__Gordon_Moore.jpg").unlink()
    images.match(conn, cfg)
    assert db.one(conn, "SELECT image_status FROM contacts WHERE full_name='Anna Smith'")["image_status"] == "none"
    assert db.one(conn, "SELECT image_status FROM contacts WHERE full_name='Gordon Moore'")["image_status"] == "manual"


def test_to_enrich_csv_order(cfg, loaded):
    path, n = images.to_enrich(loaded, cfg)
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert n == len(rows) == 6
    assert rows[0]["priority"] == "0"  # competitor/internal seeds first (high LinkedIn yield)
    bob = next(r for r in rows if r["full_name"] == "Bob Jones")
    assert bob["needs_face"] == "1" and bob["target_filename"] == "CUST__Bob_Jones.jpg"
    assert images.coverage(loaded)["customer"]["faces"] == 2


# ------------------------------------------------------------------ phones, departments, migration

from src.util import classify_department, format_phone, same_company, split_location  # noqa: E402


def test_format_phone_real_world_shapes():
    cases = {
        "082 664 6770": "+27 82 664 6770", "073-023-7765": "+27 73 023 7765", "+27 (0) 82 905 8966": "+27 82 905 8966",
        "219138030": "+27 21 913 8030",   # Excel dropped the leading 0
        "647660619.0": "+27 64 766 0619", "(082) 436 7663": "+27 82 436 7663", "061.543.6584": "+27 61 543 6584",
        "+27 83 787 5384": "+27 83 787 5384", "+27118887251": "+27 11 888 7251",
        "+267-391-6338": "+267 391 6338", "+260-211-264-544": "+260 211 264 544", "+264811277731": "+264 811 277 731",
        "082 111 2222 / 011 222 3333": "+27 82 111 2222", "011 401 6700 ext 204": "+27 11 401 6700 ext 204",
    }
    for raw, want in cases.items():
        assert format_phone(raw) == (want, True), raw
    assert format_phone("0.0") == (None, True) and format_phone(None) == (None, True)
    assert format_phone("12345") == ("12345", False)


def test_classify_department():
    assert classify_department("Account Manager") == "Sales"
    assert classify_department("Sales Director") == "Sales"
    assert classify_department("Managing Director") == "Management"
    assert classify_department("Accounts Payable Clerk") == "Finance"
    assert classify_department("Senior Buyer at Acme | Procurement") == "Procurement"
    assert classify_department("CCTV Technician") == "Technical"
    assert classify_department(None, email="accounts@acme.co.za") == "Finance"
    assert classify_department("", email="bob@acme.co.za") is None
    assert same_company("Fidelity-ADT", "Fidelity ADT (Pty) Ltd") and not same_company("Acme", "Beta Integrators")


def test_ingest_standardises_phones_and_departments(cfg, conn):
    ingest.run(cfg)
    anna = db.one(conn, "SELECT * FROM contacts WHERE full_name='Anna Smith'")
    assert anna["cell"] == "+27 82 000 0001" and json.loads(anna["extra"])["cell_raw"] == "082 000 0001"
    assert anna["department"] == "Management" and anna["department_source"] == "role"  # CEO
    bob = db.one(conn, "SELECT * FROM contacts WHERE full_name='Bob Jones'")
    assert bob["department"] == "Procurement"  # Buyer
    conn.execute("UPDATE contacts SET department='Finance', department_source='manual' WHERE id=?", [bob["id"]])
    conn.commit()
    ingest.run(cfg)
    assert db.one(conn, "SELECT department FROM contacts WHERE id=?", [bob["id"]])["department"] == "Finance"


def test_old_database_is_upgraded(tmp_path):
    """A database made before the department/status columns existed gains them on connect, data kept."""
    path = tmp_path / "old.db"
    c = db.connect(path)
    c.execute("INSERT INTO contacts(full_name, name_norm) VALUES ('Old Row', 'old row')")
    c.execute("DROP INDEX ix_contacts_status")
    c.execute("DROP INDEX ix_contacts_reports_to")
    c.execute("DROP INDEX ix_contacts_class")
    for col, _ in db.MIGRATIONS["contacts"]:
        c.execute(f"ALTER TABLE contacts DROP COLUMN {col}")
    c.commit()
    c.close()
    c = db.connect(path)
    row = db.one(c, "SELECT full_name, contact_status, employment_status, department FROM contacts")
    assert row == {"full_name": "Old Row", "contact_status": "active", "employment_status": "unknown", "department": None}


def test_split_location():
    assert split_location("Johannesburg, Gauteng, South Africa") == ("Johannesburg", "Gauteng", "South Africa")
    assert split_location("Gaborone, Botswana") == ("Gaborone", None, "Botswana")
    assert split_location("South Africa") == (None, None, "South Africa")
    assert split_location("  Cape Town ,  Western Cape , South Africa ") == ("Cape Town", "Western Cape", "South Africa")


# ------------------------------------------------------------------ v4 phase 1: account_roles + org-chart seed

def test_infer_supplier_from_title():
    assert infer_supplier_from_title("Installer Technician")
    assert infer_supplier_from_title("Regional Distributor")
    assert infer_supplier_from_title("Authorised Reseller")
    assert not infer_supplier_from_title("Sales Account Manager")
    assert not infer_supplier_from_title(None)


def test_is_subdistributor_category():
    """Found against the real Consolidated Sales Contacts sheet: 'SubD' is the only Installer
    Category value that actually means the account resells (i.e. is also a supplier) — System
    Integrator / Consultant / Installer / End-user are all just customer types."""
    assert is_subdistributor_category("SubD")
    assert is_subdistributor_category("Sub-D")
    assert is_subdistributor_category("Sub Distributor")
    assert not is_subdistributor_category("System Integrator")
    assert not is_subdistributor_category("Installer")
    assert not is_subdistributor_category("End-user")
    assert not is_subdistributor_category(None)


def test_sync_account_roles_primary_and_supplier_inference(cfg, conn):
    ingest.run(cfg)  # already calls sync_account_roles once; re-run directly to check idempotency too
    acme = db.one(conn, "SELECT id FROM accounts WHERE name='Acme Security (Pty) Ltd'")['id']
    beta = db.one(conn, "SELECT id FROM accounts WHERE name='Beta Integrators'")['id']
    assert orgchart.roles_for_account(conn, acme) == ["customer", "supplier"]  # installer_category 'SubD'
    assert orgchart.roles_for_account(conn, beta) == ["customer"]  # 'System Integrator' is not a supplier signal
    before = orgchart.roles_for_account(conn, acme)
    orgchart.sync_account_roles(conn)  # idempotent: no duplicate rows, no error
    assert orgchart.roles_for_account(conn, acme) == before


def test_add_account_role_rejects_unknown_role(conn):
    acct_id = db.insert(conn, "accounts", {"name": "X", "name_norm": "x", "segment": "customer"})
    conn.commit()
    try:
        orgchart.add_account_role(conn, acct_id, "distributor", "manual")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_seed_reports_to_builds_chain_and_is_idempotent(conn):
    seed = [
        {"name": "Big Boss", "title": "CEO", "reports_to": None},
        {"name": "Middle Manager", "title": "Director", "reports_to": "Big Boss"},
        {"name": "Departed Person", "title": "Old Role", "reports_to": "Middle Manager", "left": True},
    ]
    out = orgchart.seed_reports_to(conn, seed)
    assert out == {"nodes": 3, "matched": 0, "created": 3, "linked": 2}
    boss = db.one(conn, "SELECT * FROM contacts WHERE full_name='Big Boss'")
    mgr = db.one(conn, "SELECT * FROM contacts WHERE full_name='Middle Manager'")
    dep = db.one(conn, "SELECT * FROM contacts WHERE full_name='Departed Person'")
    assert boss["reports_to_id"] is None and boss["segment"] == "internal"
    assert mgr["reports_to_id"] == boss["id"]
    assert dep["reports_to_id"] == mgr["id"] and dep["contact_status"] == "left"

    # re-running matches the existing contacts instead of duplicating them
    out2 = orgchart.seed_reports_to(conn, seed)
    assert out2 == {"nodes": 3, "matched": 3, "created": 0, "linked": 2}
    assert db.one(conn, "SELECT count(*) n FROM contacts WHERE full_name='Big Boss'")["n"] == 1


def test_seed_reports_to_matches_existing_internal_contact(cfg, conn):
    ingest.run(cfg)  # seeds Jaco Moolman as an internal contact already
    jaco_before = db.one(conn, "SELECT id FROM contacts WHERE full_name='Jaco Moolman'")
    out = orgchart.seed_reports_to(conn, [{"name": "Jaco Moolman", "title": "Chief Executive Officer",
                                            "reports_to": None}])
    assert out == {"nodes": 1, "matched": 1, "created": 0, "linked": 0}
    jaco_after = db.one(conn, "SELECT id FROM contacts WHERE full_name='Jaco Moolman'")
    assert jaco_before["id"] == jaco_after["id"]


def test_real_org_chart_seed_file_is_well_formed():
    """The committed seed_data/elvey_org_chart.json loads and every reports_to points at a name
    that appears earlier in the list (so seed_reports_to can always resolve the manager first)."""
    seed = orgchart._load_seed()
    assert len(seed) > 50
    seen = set()
    for node in seed:
        assert node["name"] and node.get("title")
        if node.get("reports_to"):
            assert node["reports_to"] in seen, f"{node['name']} reports to {node['reports_to']} before they appear"
        seen.add(node["name"])
    assert split_location(None) == (None, None, None) and split_location("") == (None, None, None)
