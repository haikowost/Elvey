import json

import pytest

from src import db, zoho


def diff_item(d, entity, name):
    return next(i for i in d[entity + "s"] if i["name"] == name)


def test_pull_is_read_only_and_diff_statuses(cfg, loaded, fake_zoho):
    out = zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
    assert out == {"Accounts": 2, "Contacts": 1}
    assert all(c[0] == "GET" for c in fake_zoho.calls)  # never writes during pull

    d = zoho.compute_diff(loaded, cfg)
    acme = diff_item(d, "account", "Acme Security (Pty) Ltd")
    assert acme["status"] == "changed" and acme["zoho_id"] == "100" and acme["match_by"] == "name"
    changed = {f["zoho_field"] for f in acme["fields"] if f["differs"] and f["available"]}
    # Account_Name differs in spelling; Division + Elvey_Rep exist in the fake Zoho
    assert {"Division", "Elvey_Rep", "Account_Name"} <= changed
    assert not any(f["available"] for f in acme["fields"] if f["zoho_field"] == "Latest_Sellout")
    assert "Latest_Sellout" in d["missing_fields"]["Accounts"]
    assert diff_item(d, "account", "Beta Integrators")["status"] == "new"

    anna = diff_item(d, "contact", "Anna Smith")
    assert anna["match_by"] == "email" and anna["zoho_id"] == "200"
    # phone formats compare equal, same title -> only the photo is pending
    assert not [f for f in anna["fields"] if f["differs"] and f["available"]]
    assert anna["photo"]["pending"] and anna["status"] == "changed"
    assert diff_item(d, "contact", "Bob Jones")["status"] == "new"


def test_dry_run_writes_nothing(cfg, loaded, fake_zoho):
    zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
    sels = zoho.select(zoho.compute_diff(loaded, cfg), "pending")
    res = zoho.push(loaded, cfg, fake_zoho, sels, dry_run=True)
    assert res["dry_run"] and res["operations"]
    assert all(c[0] == "GET" for c in fake_zoho.calls)
    assert loaded.execute("SELECT count(*) FROM zoho_sync_log").fetchone()[0] == 0
    # unknown custom fields are never in payloads
    assert all("Latest_Sellout" not in o["payload"] for o in res["operations"])


def test_live_requires_latch(cfg, loaded, fake_zoho):
    zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
    cfg["zoho"]["live_enabled"] = False
    with pytest.raises(PermissionError):
        zoho.push(loaded, cfg, fake_zoho, [{"entity": "account", "local_id": 1}], dry_run=False)


def test_push_selected_only_then_in_sync(cfg, loaded, fake_zoho):
    zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
    cfg["zoho"]["live_enabled"] = True
    d = zoho.compute_diff(loaded, cfg)
    acme = diff_item(d, "account", "Acme Security (Pty) Ltd")
    bob = diff_item(d, "contact", "Bob Jones")
    carla = diff_item(d, "contact", "Carla Müller")
    sels = [
        {"entity": "account", "local_id": acme["local_id"], "fields": ["division"]},  # one field only
        {"entity": "contact", "local_id": bob["local_id"], "fields": None},
        {"entity": "contact", "local_id": carla["local_id"], "fields": None, "photo": True},  # parent auto-included
    ]
    res = zoho.push(loaded, cfg, fake_zoho, sels, dry_run=False)
    assert res["failed"] == 0, res["results"]

    assert fake_zoho.store["Accounts"]["100"]["Division"] == "Elvey Projects"
    assert fake_zoho.store["Accounts"]["100"]["Account_Name"] == "ACME SECURITY"  # not selected -> untouched
    bob_z = next(r for r in fake_zoho.store["Contacts"].values() if r.get("Email") == "bob@acme.co.za")
    assert bob_z["Account_Name"] == {"id": "100"} and bob_z["Last_Name"] == "Jones"
    beta = next(r for r in fake_zoho.store["Accounts"].values() if r.get("Account_Name") == "Beta Integrators")
    carla_z = next(r for r in fake_zoho.store["Contacts"].values() if r.get("Last_Name") == "Müller")
    assert carla_z["Account_Name"] == {"id": beta["id"]} and carla_z["id"] in fake_zoho.photos
    assert "Anna Smith" not in {o["name"] for o in res["operations"]}  # never selected

    # ids written back + logged
    row = db.one(loaded, "SELECT * FROM contacts WHERE full_name='Bob Jones'")
    assert row["zoho_contact_id"] == bob_z["id"] and row["zoho_last_synced"]
    actions = [r["action"] for r in db.rows(loaded, "SELECT action FROM zoho_sync_log")]
    assert actions.count("create") == 3 and "update" in actions and "photo" in actions

    # re-run: pushed records are in sync (without re-pulling, and after a fresh pull)
    for refresh in (False, True):
        if refresh:
            zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
        d2 = zoho.compute_diff(loaded, cfg)
        assert diff_item(d2, "contact", "Bob Jones")["status"] == "in-sync"
        assert diff_item(d2, "contact", "Carla Müller")["status"] == "in-sync"
        assert diff_item(d2, "account", "Beta Integrators")["status"] == "in-sync"
        acme2 = diff_item(d2, "account", "Acme Security (Pty) Ltd")
        assert {f["zoho_field"] for f in acme2["fields"] if f["differs"] and f["available"]} == {"Account_Name", "Elvey_Rep", "Pentagon_Account_Number"}


def test_empty_local_never_blanks_zoho(cfg, loaded, fake_zoho):
    fake_zoho.store["Contacts"]["200"]["Phone"] = "011 123 4567"  # local tel is empty
    zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
    anna = diff_item(zoho.compute_diff(loaded, cfg), "contact", "Anna Smith")
    phone = next(f for f in anna["fields"] if f["zoho_field"] == "Phone")
    assert phone["local_value"] is None and not phone["differs"]


def test_single_name_contact_goes_to_last_name(cfg):
    vals = zoho.local_values(cfg, "contact", {"full_name": "Siresen", "first_name": "Siresen", "last_name": None})
    assert vals["Last_Name"]["value"] == "Siresen" and vals["First_Name"]["value"] is None


def test_setup_steps(cfg):
    text = zoho.setup_steps({"Accounts": ["Elvey_Rep"], "Contacts": []}, cfg)
    assert '"Elvey Rep"' in text and "Elvey_Rep" in text
    assert zoho.setup_steps({"Accounts": []}, cfg) == "All mapped fields exist in Zoho."


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, pages):
        self.pages, self.posts, self.gets = pages, 0, []

    def post(self, url, params=None, timeout=None):
        self.posts += 1
        assert "accounts.zoho.eu" in url and params["grant_type"] == "refresh_token"
        return FakeResp(200, {"access_token": f"tok{self.posts}", "expires_in": 3600, "api_domain": "https://www.zohoapis.eu"})

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.gets.append((url, dict(kw.get("params") or {}), headers["Authorization"]))
        if len(self.gets) == 1:
            return FakeResp(401, {"code": "INVALID_TOKEN"})
        return self.pages.pop(0)


def test_client_refresh_and_pagination():
    pages = [FakeResp(200, {"data": [{"id": "1"}], "info": {"more_records": True, "next_page_token": "abc"}}),
             FakeResp(200, {"data": [{"id": "2"}], "info": {"more_records": False}})]
    s = FakeSession(pages)
    c = zoho.ZohoClient("id", "secret", "refresh", "eu", pause_s=0, session=s)
    got = list(c.iter_records("Accounts", ["id", "Account_Name"]))
    assert [r["id"] for r in got] == ["1", "2"]
    assert s.posts == 2  # initial + re-auth after 401
    assert s.gets[-1][1].get("page_token") == "abc" and s.gets[-1][0].startswith("https://www.zohoapis.eu/crm/v3/")
    with pytest.raises(zoho.ZohoError):
        zoho.ZohoClient("", "", "", "com")


def test_department_mapped_and_inactive_left_out(cfg, loaded, fake_zoho):
    fake_zoho.FIELDS = {**fake_zoho.FIELDS, "Contacts": fake_zoho.FIELDS["Contacts"] + ["Department"]}
    zoho.pull(loaded, cfg, fake_zoho, log=lambda *a: None)
    d = zoho.compute_diff(loaded, cfg)
    bob = diff_item(d, "contact", "Bob Jones")
    dept = next(f for f in bob["fields"] if f["zoho_field"] == "Department")
    assert dept["local_value"] == "Procurement" and dept["available"]
    loaded.execute("UPDATE contacts SET contact_status='left' WHERE full_name='Bob Jones'")
    loaded.commit()
    assert "Bob Jones" not in {i["name"] for i in zoho.compute_diff(loaded, cfg)["contacts"]}
