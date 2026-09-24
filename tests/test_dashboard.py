import json

from fastapi.testclient import TestClient

from src import dashboard, db


def test_dashboard_endpoints(cfg, loaded):
    app = dashboard.create_app(cfg)
    c = TestClient(app)
    assert "People Tree" in c.get("/").text

    tree = c.get("/api/people").json()
    assert set(tree) == {"competitor", "internal", "customer"}
    acme = tree["customer"][0]
    assert acme["title"] == "Acme Security (Pty) Ltd" and acme["rank"] == 1 and "Tasha: High" in acme["tags"]
    anna = next(p for p in acme["people"] if p["name"] == "Anna Smith")
    assert anna["face"] == "/faces/CUST__Anna_Smith.jpg"
    assert c.get(anna["face"]).headers["content-type"] == "image/jpeg"
    assert anna["company"] == "Acme Security (Pty) Ltd" and anna["mobile"] == "+27 82 000 0001" and "enriched_at" in anna
    assert c.get("/faces/..%2Fconsolidated.xlsx").status_code == 404
    assert {g["title"] for g in tree["competitor"]} == {"Duxbury", "Reditron"}

    stats = c.get("/api/stats").json()
    assert stats["contacts"] == 6 and stats["live_enabled"] is False

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
