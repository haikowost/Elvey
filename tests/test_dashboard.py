import json

from fastapi.testclient import TestClient

from src import dashboard, db


def test_dashboard_endpoints(cfg, loaded):
    app = dashboard.create_app(cfg)
    c = TestClient(app)
    assert "People Tree" in c.get("/").text

    tree = c.get("/api/people").json()
    assert set(tree) == {"competitor", "internal", "customer", "supplier"}
    acme = tree["customer"][0]
    assert acme["title"] == "Acme Security (Pty) Ltd" and acme["rank"] == 1 and "Tasha: High" in acme["tags"]
    anna = next(p for p in acme["people"] if p["name"] == "Anna Smith")
    assert anna["face"] == "/faces/CUST__Anna_Smith.jpg"
    assert c.get(anna["face"]).headers["content-type"] == "image/jpeg"
    assert anna["company"] == "Acme Security (Pty) Ltd" and anna["mobile"] == "+27 82 000 0001" and "enriched_at" in anna
    assert c.get("/faces/..%2Fconsolidated.xlsx").status_code == 404
    assert {g["title"] for g in tree["competitor"]} == {"Duxbury", "Reditron"}
    # v3: rows without an AM/allocation column (this old relational fixture) count as allocated
    assert anna["allocated"] is True and anna["allocated_rep"] is None
    # v4: Acme has an axis_partner hint -> also tagged supplier; the group carries its account_roles
    assert acme["account_roles"] == ["customer", "supplier"]
    assert anna["contact_class"] is None and anna["reports_to"] is None

    accts = c.get("/api/accounts").json()
    assert next(a for a in accts if a["name"] == "Acme Security (Pty) Ltd")["roles"] == ["customer", "supplier"]
    assert next(a for a in accts if a["name"] == "Beta Integrators")["roles"] == ["customer"]

    stats = c.get("/api/stats").json()
    assert stats["contacts"] == 6 and stats["live_enabled"] is False
    assert stats["allocated"] == 6 and stats["unallocated"] == 0 and stats["reps"] == [] and stats["categories"] == []
    assert stats["contact_classes"] == {"unclassified": 6}
    assert stats["account_roles"]["customer"] == 2 and stats["account_roles"]["supplier"] == 1

    d = c.get("/api/zoho/diff").json()
    assert d["pulled_at"] is None and d["summary"]["accounts"]["new"] >= 2
    item = d["contacts"][0]
    r = c.post("/api/zoho/push", json={"selections": [{"entity": "contact", "local_id": item["local_id"]}]}).json()
    assert r["dry_run"] is True and r["operations"]
    r = c.post("/api/zoho/push", json={"selections": [{"entity": "contact", "local_id": item["local_id"]}], "live": True})
    assert r.status_code in (400, 403)  # no creds / latch closed — never writes
    assert c.get("/api/zoho/log").json() == []


def test_contact_card_edits(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    bob = next(p for g in c.get("/api/people").json()["customer"] for p in g["people"] if p["name"] == "Bob Jones")

    r = c.post(f"/api/contacts/{bob['id']}", json={"department": "Finance", "contact_status": "not_relevant",
                                                   "status_note": "accounts only"})
    assert r.status_code == 200 and r.json()["contact"]["department_source"] == "manual"
    assert c.post(f"/api/contacts/{bob['id']}", json={"department": "Astrology"}).status_code == 400
    assert c.post(f"/api/contacts/{bob['id']}", json={"contact_status": "gone"}).status_code == 400

    # LinkedIn flagged a move: accept it -> re-allocated, old account remembered, active again
    beta = next(a for a in c.get("/api/accounts").json() if a["name"] == "Beta Integrators")
    loaded.execute("UPDATE contacts SET employment_status='moved', linkedin_current_company='Beta Integrators', "
                   "moved_to_account_id=? WHERE id=?", [beta["id"], bob["id"]])
    loaded.commit()
    moved = c.post(f"/api/contacts/{bob['id']}", json={"move_to_account_id": beta["id"]}).json()["contact"]
    assert moved["account_id"] == beta["id"] and moved["employment_status"] == "current" and moved["contact_status"] == "active"
    assert json.loads(moved["extra"])["previous_accounts"][0]["name"] == "Acme Security (Pty) Ltd"

    # new employer not in the DB yet -> create it
    made = c.post(f"/api/contacts/{bob['id']}", json={"create_account": "Zeta Security"}).json()["contact"]
    assert any(a["name"] == "Zeta Security" and a["id"] == made["account_id"] for a in c.get("/api/accounts").json())

    # LinkedIn wrong -> keep
    loaded.execute("UPDATE contacts SET employment_status='moved', linkedin_current_company='Nope' WHERE id=?", [bob["id"]])
    loaded.commit()
    kept = c.post(f"/api/contacts/{bob['id']}", json={"keep_account": True}).json()["contact"]
    assert kept["employment_status"] == "current" and kept["account_id"] == made["account_id"]

    tree = c.get("/api/people").json()
    bob2 = next(p for g in tree["customer"] for p in g["people"] if p["name"] == "Bob Jones")
    assert bob2["company"] == "Zeta Security" and len(bob2["previous_accounts"]) == 2
    assert "departments" in c.get("/api/stats").json()


def test_dual_role_account_appears_under_both_branch_tabs(cfg, loaded):
    """Acme has account_roles customer+supplier (axis_partner hint) -> v4 role-based tab
    membership shows the same account, and the same people, under both the Customers and
    Suppliers branches, until segment_override starts splitting individual contacts."""
    c = TestClient(dashboard.create_app(cfg))
    tree = c.get("/api/people").json()
    cust_acme = next(g for g in tree["customer"] if g["title"] == "Acme Security (Pty) Ltd")
    supp_acme = next(g for g in tree["supplier"] if g["title"] == "Acme Security (Pty) Ltd")
    assert cust_acme["account_roles"] == supp_acme["account_roles"] == ["customer", "supplier"]
    assert {p["name"] for p in cust_acme["people"]} == {p["name"] for p in supp_acme["people"]}
    # Beta has no supplier hint -> customer only, not duplicated into Suppliers
    assert not any(g["title"] == "Beta Integrators" for g in tree["supplier"])
    assert any(g["title"] == "Beta Integrators" for g in tree["customer"])


def test_contact_class_and_reports_to_edits(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    people = {p["name"]: p for g in c.get("/api/people").json()["customer"] for p in g["people"]}
    bob, anna = people["Bob Jones"], people["Anna Smith"]

    r = c.post(f"/api/contacts/{bob['id']}", json={"contact_class": "engaged"})
    assert r.status_code == 200 and r.json()["contact"]["contact_class"] == "engaged"
    assert c.post(f"/api/contacts/{bob['id']}", json={"contact_class": "urgent"}).status_code == 400
    cleared = c.post(f"/api/contacts/{bob['id']}", json={"contact_class": ""}).json()["contact"]
    assert cleared["contact_class"] is None

    r = c.post(f"/api/contacts/{bob['id']}", json={"reports_to_id": anna["id"]})
    assert r.status_code == 200
    tree = c.get("/api/people").json()
    bob2 = next(p for g in tree["customer"] for p in g["people"] if p["name"] == "Bob Jones")
    assert bob2["reports_to"] == {"id": anna["id"], "name": "Anna Smith"}

    assert c.post(f"/api/contacts/{bob['id']}", json={"reports_to_id": bob["id"]}).status_code == 400
    assert c.post(f"/api/contacts/{bob['id']}", json={"reports_to_id": 999999}).status_code == 400

    cleared2 = c.post(f"/api/contacts/{bob['id']}", json={"reports_to_id": 0}).json()["contact"]
    assert cleared2["reports_to_id"] is None


def test_engaged_contacts_sort_first_within_their_group(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    acme = next(g for g in c.get("/api/people").json()["customer"] if g["title"] == "Acme Security (Pty) Ltd")
    bob = next(p for p in acme["people"] if p["name"] == "Bob Jones")
    assert bob["priority"] is not None  # Bob would otherwise rank behind Anna on priority alone
    c.post(f"/api/contacts/{bob['id']}", json={"contact_class": "engaged"})
    acme2 = next(g for g in c.get("/api/people").json()["customer"] if g["title"] == "Acme Security (Pty) Ltd")
    assert acme2["people"][0]["name"] == "Bob Jones"


def test_verify_endpoints(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    bob = next(p for g in c.get("/api/people").json()["customer"] for p in g["people"] if p["name"] == "Bob Jones")
    loaded.execute("UPDATE contacts SET enrich_status='done', linkedin_summary=? WHERE id=?",
                   ["Accessibility Talent Solutions Community Guidelines", bob["id"]])
    loaded.commit()

    rep = c.get("/api/verify").json()
    assert [e["id"] for e in rep["footer_polluted"]] == [bob["id"]]
    still = c.get("/api/zoho/diff").json()  # GET must never have written anything
    assert db.one(loaded, "SELECT enrich_status FROM contacts WHERE id=?", [bob["id"]])["enrich_status"] == "done"

    fixed = c.post("/api/verify").json()
    assert fixed["requeued"] == [bob["id"]]
    assert db.one(loaded, "SELECT enrich_status FROM contacts WHERE id=?", [bob["id"]])["enrich_status"] == "pending"


def test_contact_card_set_linkedin_url(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    bob = next(p for g in c.get("/api/people").json()["customer"] for p in g["people"] if p["name"] == "Bob Jones")
    loaded.execute("UPDATE contacts SET enrich_status='no_profile', linkedin_summary=NULL WHERE id=?", [bob["id"]])
    loaded.commit()

    r = c.post(f"/api/contacts/{bob['id']}", json={"linkedin_url": "https://www.linkedin.com/in/marie-deysel-1503a53a/"})
    assert r.status_code == 200
    contact = r.json()["contact"]
    assert contact["linkedin_contact_url"] == "https://www.linkedin.com/in/marie-deysel-1503a53a/"
    assert contact["enrich_status"] == "pending"

    bad = c.post(f"/api/contacts/{bob['id']}", json={"linkedin_url": "not a url"})
    assert bad.status_code == 400


def test_chat_queue_and_correction_flow(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    bob = next(p for g in c.get("/api/people").json()["customer"] for p in g["people"] if p["name"] == "Bob Jones")
    loaded.execute("UPDATE contacts SET enrich_status='no_profile' WHERE id=?", [bob["id"]])
    loaded.commit()

    queue = c.get("/api/chat/queue").json()
    assert queue and queue[0]["id"] == bob["id"] and "couldn't find" in queue[0]["reason"]

    r = c.post("/api/chat", json={"contact_id": bob["id"], "message": "https://www.linkedin.com/in/bob-real/"})
    body = r.json()
    assert body["action"] == "apply" and "Re-queued" in body["reply"]
    assert body["contact"]["linkedin_contact_url"] == "https://www.linkedin.com/in/bob-real/"
    assert body["contact"]["enrich_status"] == "pending"

    # resolved — no longer in the queue
    assert bob["id"] not in {q["id"] for q in c.get("/api/chat/queue").json()}

    r2 = c.post("/api/chat", json={"contact_id": bob["id"], "message": "skip"})
    assert r2.json() == {"action": "skip", "reply": f"Skipped Bob Jones.", "contact": None}

    r3 = c.post("/api/chat", json={"contact_id": bob["id"], "message": "not relevant"})
    assert r3.json()["contact"]["contact_status"] == "not_relevant"

    assert c.post("/api/chat", json={"contact_id": 999999, "message": "left"}).status_code == 404


def test_chat_accepts_unknown_department_gracefully(cfg, loaded):
    c = TestClient(dashboard.create_app(cfg))
    bob = next(p for g in c.get("/api/people").json()["customer"] for p in g["people"] if p["name"] == "Bob Jones")
    r = c.post("/api/chat", json={"contact_id": bob["id"], "message": "gibberish nonsense"})
    body = r.json()
    assert body["action"] == "unclear" and body["contact"] is None
    assert db.one(loaded, "SELECT contact_status FROM contacts WHERE id=?", [bob["id"]])["contact_status"] == "active"
