from fastapi.testclient import TestClient

from src import dashboard


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
