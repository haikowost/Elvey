"""2026-10 KYC round: Key tiers + INT/SUP contact creation, skip-complete harvest queue, the token-free
"Update from LinkedIn" endpoint, the Send to KYC bookmarklet capture (parsing + matching on fixture
HTML), the people snapshot and the scheduling scripts."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from src import capture, dashboard, db, export, harvest, ingest, priority
from src.harvest import Result

ROOT = Path(__file__).resolve().parent.parent
HEADER = ["kyc_order", "contact_id", "account_id", "full_name", "company", "email", "tier", "key_reason", "score"]
ROWS = [
    [1, "INT-001", "", "Valerie Bingham", "Elvey Group", "", "Key: Internal", "Elvey staff (org chart)", ""],
    [2, "INT-002", "", "Jaco Moolman", "Elvey Group", "", "Key: Internal", "Elvey staff (org chart)", ""],  # already in DB
    [3, "SUP-MS-1", "", "George Psoulis", "Milestone Systems (SA)", "", "Key: Supplier", "Vendor team", ""],
    [4, "ZZZ", "A0003", "Dan Out", "Gamma Systems", "", "Key: BD", "BD end user", 61],
    [5, "C8", "", "Leandro da Cunha", "Duxbury", "", "Key: Competitor", "Competitor", 40],
    [6, "C1", "A0001", "Anna Smith", "Acme Security (Pty) Ltd", "anna@acme.co.za", "P1 Key", "", 80],
    [7, "C4", "A0002", "Carla Müller", "Beta Integrators", "", "P2 Active", "", 50],
    [8, "C2", "A0001", "Bob Jones", "Acme Security (Pty) Ltd", "bob@acme.co.za", "P3 Reference", "", 30],
    [9, "C9", "", "Gordon Moore", "Reditron", "", "P4 Low relevance", "", 5],
]


def _csv(path, rows=ROWS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)
    return path


@pytest.fixture
def prio(cfg, conn, tmp_path):
    ingest.run(cfg, all_rows=True)
    stats = priority.import_file(conn, _csv(tmp_path / "kyc_priority.csv"))
    return stats


def _c(conn, name):
    return db.one(conn, "SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id "
                        "WHERE c.full_name = ?", [name])


# --------------------------------------------------------------------------- priority import

def test_int_and_sup_rows_create_contacts_idempotently(cfg, conn, prio, tmp_path):
    assert prio["created"] == 2 and prio["unmatched"] == 0
    val, george, jaco = _c(conn, "Valerie Bingham"), _c(conn, "George Psoulis"), _c(conn, "Jaco Moolman")
    assert (val["segment"], val["source_id"], val["kyc_tier"]) == ("internal", "INT-001", "Key: Internal")
    assert (george["segment"], george["kyc_tier"], george["company"]) == ("supplier", "Key: Supplier", "Milestone Systems (SA)")
    assert "supplier" in [r["role"] for r in db.rows(conn, "SELECT role FROM account_roles WHERE account_id = ?",
                                                     [george["account_id"]])]
    assert jaco["kyc_tier"] == "Key: Internal"   # matched the existing internal contact, not duplicated
    assert db.one(conn, "SELECT count(*) AS n FROM contacts WHERE name_norm = 'jaco moolman'")["n"] == 1
    n = db.one(conn, "SELECT count(*) AS n FROM contacts")["n"]
    again = priority.import_file(conn, tmp_path / "kyc_priority.csv")
    assert again["created"] == 0 and db.one(conn, "SELECT count(*) AS n FROM contacts")["n"] == n


def test_seed_csv_has_the_key_tiers_and_new_people():
    with open(priority.SEED_CSV, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    tiers = {r["tier"] for r in rows}
    assert set(priority.KEY_TIERS) <= tiers
    assert sum(r["contact_id"].startswith("INT-") for r in rows) == 13
    assert {r["full_name"] for r in rows if r["contact_id"].startswith("SUP-MS-")} == {
        "George Psoulis", "Arlette Yomi", "Melinda Fernihough", "Rikus Terblanche", "Llewellyn Davies"}
    assert all(priority.new_contact_segment(r) for r in rows if r["contact_id"][:4] in ("INT-", "SUP-"))


# --------------------------------------------------------------------------- harvest order + skip-complete

def test_queue_order_key_tiers_first(cfg, conn, prio):
    names = [r["full_name"] for r in harvest.queue(conn, cfg)]
    assert names[:7] == ["Valerie Bingham", "Jaco Moolman", "George Psoulis", "Dan Out", "Leandro da Cunha",
                         "Anna Smith", "Carla Müller"]
    assert "Gordon Moore" not in names                     # P4 skipped
    assert [r["full_name"] for r in harvest.queue(conn, cfg, tier="key")] == names[:5]
    assert [r["full_name"] for r in harvest.queue(conn, cfg, tier="p1")] == names[:6]
    # a Y-pinned P2 contact goes ahead of P1, but never ahead of a Key tier
    conn.execute("UPDATE contacts SET kyc_pinned = 1 WHERE full_name = 'Carla Müller'")
    conn.commit()
    names = [r["full_name"] for r in harvest.queue(conn, cfg)]
    assert names[5:7] == ["Carla Müller", "Anna Smith"]


def _make_complete(conn, name, days_ago=0):
    conn.execute(f"""UPDATE contacts SET role = 'Buyer', linkedin_profile_url = 'https://www.linkedin.com/in/x-{days_ago}',
                     image_status = 'downloaded', linkedin_experience = '[{{"title": "Buyer"}}]', enrich_status = 'done',
                     enrich_last_at = datetime('now', 'localtime', '-{days_ago} days') WHERE full_name = ?""", [name])
    conn.commit()


def test_complete_contacts_are_skipped_until_stale(cfg, conn, prio):
    _make_complete(conn, "Anna Smith")
    assert "Anna Smith" not in [r["full_name"] for r in harvest.queue(conn, cfg)]
    assert harvest.is_complete(_c(conn, "Anna Smith"))
    _make_complete(conn, "Anna Smith", days_ago=100)
    assert "Anna Smith" in [r["full_name"] for r in harvest.queue(conn, cfg)]   # stale: refreshed
    assert not harvest.is_complete(_c(conn, "Anna Smith"))
    # done recently but no photo yet: only the missing photo is worth a visit -> queued, face requested
    _make_complete(conn, "Carla Müller")
    conn.execute("UPDATE contacts SET image_status = 'none' WHERE full_name = 'Carla Müller'")
    conn.commit()
    carla = next(r for r in harvest.queue(conn, cfg) if r["full_name"] == "Carla Müller")
    assert carla["image_status"] == "none"
    # done recently, nothing new to get (LinkedIn had no photo): not re-visited every run
    conn.execute("UPDATE contacts SET image_status = 'no_photo' WHERE full_name = 'Carla Müller'")
    conn.commit()
    assert "Carla Müller" not in [r["full_name"] for r in harvest.queue(conn, cfg)]
    # failed past max_attempts: given up on
    conn.execute("UPDATE contacts SET enrich_status = 'failed', enrich_attempts = 5 WHERE full_name = 'Dan Out'")
    conn.commit()
    assert "Dan Out" not in [r["full_name"] for r in harvest.queue(conn, cfg)]
    # ...but an explicit forced update (the dashboard button) always runs
    dan = _c(conn, "Dan Out")["id"]
    assert [r["id"] for r in harvest.queue(conn, cfg, ids=[dan], force=True)] == [dan]


def test_revisit_never_wipes_existing_work_history(cfg, conn, prio):
    anna = _c(conn, "Anna Smith")
    conn.execute("""UPDATE contacts SET linkedin_experience = '[{"title": "CEO", "company": "Acme"}]' WHERE id = ?""", [anna["id"]])
    conn.commit()
    res = Result("done", profile_url="https://www.linkedin.com/in/anna", profile_name="Anna Smith", headline="CEO at Acme")
    harvest.apply_result(conn, cfg, _c(conn, "Anna Smith"), res, need_face=False)
    assert json.loads(_c(conn, "Anna Smith")["linkedin_experience"])[0]["title"] == "CEO"


# --------------------------------------------------------------------------- "Update from LinkedIn" (no tokens)

class OneDriver:
    def __init__(self, res):
        self.res, self.seen = res, []

    def fetch(self, contact, need_face):
        self.seen.append((contact["full_name"], need_face))
        return self.res

    def close(self):
        pass


def test_manual_update_endpoint_enriches_one_contact_and_respects_cap(cfg, conn, prio):
    res = Result("done", profile_url="https://www.linkedin.com/in/dan-out", profile_name="Dan Out",
                 headline="Security Manager at Gamma Systems",
                 experience=[{"title": "Security Manager", "company": "Gamma Systems", "dates": "2019 - Present"}])
    drv = OneDriver(res)
    app = dashboard.create_app(cfg, driver_factory=lambda: drv, snapshot_async=False)
    c = TestClient(app)
    dan = _c(conn, "Dan Out")["id"]
    r = c.post(f"/api/contacts/{dan}/harvest")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contact"]["role"] == "Security Manager" and body["result"].startswith("done")
    assert body["contact"]["experience"][0]["company"] == "Gamma Systems"
    assert drv.seen == [("Dan Out", True)]
    assert c.get("/api/harvest/status").json()["last"]["name"] == "Dan Out"
    assert (Path(cfg.kyc_folder) / export.SNAPSHOT_JSON).exists()   # snapshot refreshed after the update
    assert c.post("/api/contacts/999999/harvest").status_code == 404
    cfg["harvest"]["daily_cap"] = 1   # that visit used today's (test) cap
    r = c.post(f"/api/contacts/{dan}/harvest")
    assert r.status_code == 429 and "cap" in r.json()["detail"]


# --------------------------------------------------------------------------- Send to KYC bookmarklet

PROFILE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Bob Jones | LinkedIn</title></head><body><main>
 <section><img class="pv-top-card-profile-picture__image--show" width="100" height="100"
      src="https://media.licdn.com/dms/image/v2/profile-displayphoto-shrink_200_200/bob.jpg"><h1>Bob Jones</h1>
   <div class="text-body-medium">Senior Buyer at Acme Security</div>
   <div>Johannesburg, Gauteng, South Africa</div></section>
 <section><div id="about"></div><h2>About</h2><span aria-hidden="true">About</span><span aria-hidden="true">Procurement lead for security projects.</span></section>
 <section><div id="experience"></div><h2>Experience</h2><ul>
   <li><span aria-hidden="true">Senior Buyer</span><span aria-hidden="true">Acme Security · Full-time</span>
       <span aria-hidden="true">Jan 2021 - Present · 3 yrs</span></li>
   <li><span aria-hidden="true">Buyer</span><span aria-hidden="true">Beta Integrators · Full-time</span>
       <span aria-hidden="true">Mar 2017 - Dec 2020 · 3 yrs</span></li>
 </ul></section>
</main></body></html>"""
PROFILE_URL = "https://www.linkedin.com/in/bob-jones-123/"


def _bookmarklet_js(port=8765) -> str:
    href = dashboard.bookmarklet_source(port)
    assert href.startswith("javascript:")
    return unquote(href[len("javascript:"):])


@pytest.fixture(scope="module")
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    exe = os.environ.get("KYC_TEST_CHROMIUM") or next(
        (p for p in ("/opt/pw-browsers/chromium", "/usr/bin/chromium") if os.path.exists(p)), None)
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**({"executable_path": exe} if exe and os.path.isfile(exe) else {}))
        except Exception as e:  # pragma: no cover
            pytest.skip(f"no chromium: {e}")
        yield browser
        browser.close()


def _run_bookmarklet(browser, html=PROFILE_HTML, url=PROFILE_URL) -> dict:
    """Open fixture HTML at a linkedin.com/in/ URL, click the bookmarklet, return what it POSTed."""
    page = browser.new_page()
    sent: dict = {}
    page.route(url, lambda route: route.fulfill(status=200, content_type="text/html", body=html))

    def api(route):
        sent["origin"] = route.request.headers.get("origin")
        sent["body"] = json.loads(route.request.post_data)
        route.fulfill(status=200, content_type="application/json",
                      headers={"Access-Control-Allow-Origin": "https://www.linkedin.com"},
                      body=json.dumps({"status": "saved", "contact": {"name": "Bob Jones"}}))
    page.route("http://127.0.0.1:8765/api/capture", api)
    page.goto(url)
    page.evaluate(_bookmarklet_js())
    page.wait_for_selector("#elvey-kyc-toast:has-text('Saved')", timeout=5000)
    page.close()
    return sent


def test_bookmarklet_reads_the_visible_profile(chromium):
    sent = _run_bookmarklet(chromium)
    assert sent["origin"] == "https://www.linkedin.com"
    p = sent["body"]
    assert p["url"] == PROFILE_URL and p["photo_src"].startswith("https://media.licdn.com/")
    assert p["data"]["name"] == "Bob Jones" and p["data"]["headline"] == "Senior Buyer at Acme Security"
    res = capture.to_result(capture.sanitize(p))
    assert res.status == "done" and res.location == "Johannesburg, Gauteng, South Africa"
    assert [e["company"] for e in res.experience] == ["Acme Security", "Beta Integrators"]


def _payload(name="Bob Jones", url=PROFILE_URL, company="Acme Security", title="Senior Buyer", photo=""):
    """What the bookmarklet sends (same shape as harvest.JS_PROFILE's output)."""
    return {"url": url, "photo_src": photo, "data": {
        "name": name, "headline": f"{title} at {company}", "about": "", "title": f"{name} | LinkedIn",
        "experience": [[title, f"{company} · Full-time", "Jan 2021 - Present · 3 yrs", "Johannesburg"]],
        "topLines": [name, f"{title} at {company}", "Johannesburg, Gauteng, South Africa"], "text": [], "sections": []}}


def test_capture_endpoint_cors_match_and_apply(cfg, conn, prio, monkeypatch):
    monkeypatch.setattr(capture, "fetch_photo", lambda src, cfg: b"\xff\xd8fakejpeg" if src else None)
    c = TestClient(dashboard.create_app(cfg, snapshot_async=False))
    li = {"Origin": "https://www.linkedin.com"}
    pre = c.options("/api/capture", headers={**li, "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Private-Network": "true"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "https://www.linkedin.com"
    assert pre.headers["access-control-allow-private-network"] == "true"
    assert c.options("/api/capture", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/capture", json=_payload(), headers={"Origin": "https://evil.example"}).status_code == 403
    assert "access-control-allow-origin" not in c.get("/api/people", headers=li).headers   # CORS on capture only

    # name + current company -> Bob Jones at Acme; role from what Haiko saw beats the sheet's 'Buyer'
    r = c.post("/api/capture", json=_payload(photo="https://media.licdn.com/dms/image/bob.jpg"), headers=li)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://www.linkedin.com"
    j = r.json()
    assert j["status"] == "saved" and j["how"] == "name+company" and j["contact"]["name"] == "Bob Jones"
    bob = _c(conn, "Bob Jones")
    assert (bob["role"], bob["role_source"]) == ("Senior Buyer", "linkedin")
    assert capture.slug(bob["linkedin_profile_url"]) == capture.slug(bob["linkedin_contact_url"]) == "bob-jones-123"
    assert bob["image_status"] == "downloaded" and bob["enrich_status"] == "done"
    assert harvest.used_today(conn) == 0          # captures never count against the daily cap
    # the same profile again now matches on the profile URL
    assert c.post("/api/capture", json=_payload(company="Somewhere Else"), headers=li).json()["how"] == "profile url"

    # not at the company we know -> waits for the picker, with Carla as a candidate
    j = c.post("/api/capture", json=_payload("Carla Muller", "https://www.linkedin.com/in/carla-m/", "Delta Corp",
                                             "Project Lead"), headers=li).json()
    assert j["status"] == "needs_match" and j["candidates"][0]["name"] == "Carla Müller"
    assert [x["capture_id"] for x in c.get("/api/captures").json()] == [j["capture_id"]]
    assert c.get("/api/kyc/progress").json()["captures_pending"] == 1
    done = c.post(f"/api/captures/{j['capture_id']}/apply", json={"contact_id": j["candidates"][0]["id"]}).json()
    assert done.get("status") == "saved", done
    assert done["status"] == "saved" and _c(conn, "Carla Müller")["role"] == "Project Lead"
    assert c.get("/api/captures").json() == []

    # someone new -> created at the company the profile shows
    j = c.post("/api/capture", json=_payload("Zanele Dube", "https://www.linkedin.com/in/zdube/", "Omega Fire",
                                             "Engineer"), headers=li).json()
    assert j["status"] == "needs_match"
    done = c.post(f"/api/captures/{j['capture_id']}/apply", json={"create": True}).json()
    z = _c(conn, "Zanele Dube")
    assert done["how"] == "created" and z["company"] == "Omega Fire" and z["role"] == "Engineer"

    # junk page
    assert c.post("/api/capture", json={"url": "https://www.linkedin.com/feed/", "data": {}}, headers=li).json()["status"] == "failed"


def test_bookmarklet_and_capture_pages_served(cfg, conn, prio):
    c = TestClient(dashboard.create_app(cfg))
    page = c.get("/bookmarklet").text
    assert 'href="javascript:' in page and "Send to KYC" in page
    js = _bookmarklet_js()
    assert "127.0.0.1:8765" in js and not any(x in js for x in ("__PORT__", "__JS_PROFILE__", "__PHOTO_SELECTOR__"))
    assert "captures" in c.get("/capture").text


# --------------------------------------------------------------------------- snapshot + progress

def test_snapshot_written_to_configured_dir(cfg, conn, prio, tmp_path):
    out = tmp_path / "drive" / "KYC snapshot"
    out.parent.mkdir()
    cfg.setdefault("exports", {})["snapshot_dir"] = str(out)
    _make_complete(conn, "Anna Smith")
    conn.execute("UPDATE contacts SET image_filename = 'CUST__Anna_Smith.jpg' WHERE full_name = 'Anna Smith'")
    conn.commit()
    res = export.write_snapshot(conn, cfg)
    assert Path(res["json"]).parent == out and Path(res["csv"]).exists()
    data = json.loads((out / export.SNAPSHOT_JSON).read_text(encoding="utf-8"))
    anna = next(p for p in data["people"] if p["name"] == "Anna Smith")
    assert anna["tier"] == "P1 Key" and anna["complete"] and anna["photo"].startswith("data:image/jpeg;base64,")
    assert data["people"][0]["name"] == "Valerie Bingham"      # priority order
    assert {"source": f"c{anna['id'][1:]}", "target": anna["account_id"], "rel": "works_at"} in data["links"]
    assert any(t["tier"] == "Key: Internal" for t in data["tiers"])
    with open(out / export.SNAPSHOT_CSV, encoding="utf-8-sig") as fh:
        assert next(csv.DictReader(fh))["name"] == "Valerie Bingham"
    # no configured folder whose parent exists -> Drive KYC folder (paths.kyc_folder) -> else data/exports
    cfg["exports"]["snapshot_dir"] = ""
    assert export.snapshot_dir(cfg) == cfg.kyc_folder
    cfg["exports"]["snapshot_dir"] = "Z:/not/mounted/here"
    assert export.snapshot_dir(cfg) == cfg.data_dir / "exports"


def test_progress_by_tier_done_pending_failed_and_schedule(cfg, conn, prio):
    conn.execute("UPDATE contacts SET enrich_status = 'no_profile' WHERE full_name = 'George Psoulis'")
    conn.execute("UPDATE contacts SET enrich_status = 'done' WHERE full_name = 'Valerie Bingham'")
    conn.commit()
    p = TestClient(dashboard.create_app(cfg)).get("/api/kyc/progress").json()
    tiers = {t["tier"]: t for t in p["tiers"]}
    assert [t["tier"] for t in p["tiers"]][:4] == list(priority.KEY_TIERS)
    assert (tiers["Key: Internal"]["enriched"], tiers["Key: Internal"]["pending"]) == (1, 1)
    assert (tiers["Key: Supplier"]["failed"], tiers["Key: Supplier"]["pending"]) == (1, 0)
    assert p["schedule"]["mode"] in ("manual only", "scheduled") and "old_task_present" in p["schedule"]


def test_schedule_status_reads_marker_off_windows(cfg, monkeypatch):
    from src import schedule

    monkeypatch.setattr(schedule.os, "name", "posix")
    assert schedule.status(cfg.data_dir, use_cache=False)["mode"] == "manual only"
    (cfg.data_dir / "schedule.json").write_text(json.dumps({"task": schedule.TASK, "time": "06:40"}), encoding="utf-8")
    s = schedule.status(cfg.data_dir, use_cache=False)
    assert s["mode"] == "scheduled" and "06:40" in s["next_run"]


# --------------------------------------------------------------------------- scripts

def test_scheduling_scripts():
    scripts = ROOT / "scripts"
    un = (scripts / "unschedule_kyc.ps1").read_text(encoding="utf-8")
    assert 'schtasks /Delete /TN "$name" /F' in un and "'Elvey LinkedIn harvest'" in un
    assert "/Query" in un and "Not scheduled" in un                       # tolerates not-found
    sch = (scripts / "schedule_kyc.ps1").read_text(encoding="utf-8")
    assert "-Daily" in sch and "run_kyc.ps1" in sch and "Split-Path -Parent $PSScriptRoot" in sch
    assert "HaikoWostmann" not in sch and "unschedule_kyc.ps1" in sch
    run = (scripts / "run_kyc.ps1").read_text(encoding="utf-8")
    assert "Elvey LinkedIn harvest" in run and "unschedule_kyc.ps1" in run and "src.export snapshot" in run
    assert "'key'" in run
    old = (scripts / "run_harvest.ps1").read_text(encoding="utf-8")
    assert "src.harvest" not in old                                       # retired: can't harvest any more
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/SC MINUTE /MO 120" not in readme and "schedule_kyc.ps1" in readme
