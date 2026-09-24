import csv
import json

from src import db, images, ingest
from src.util import face_filename, norm_company, parse_face_filename, parse_money, slug, split_name


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
    assert bob["role"] == "Buyer" and bob["cell"] == "083 111 2222" and bob["priority"] == 2
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
