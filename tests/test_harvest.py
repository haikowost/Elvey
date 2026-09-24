import io

import pytest
import json

from PIL import Image

from src import db, harvest
from src.harvest import Result, StopHarvest, build_summary, is_stop_page, parse_experience


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (240, 240), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


def test_parse_experience_single_and_grouped():
    entries = [
        ["Head of Security", "Acme Security · Full-time", "Jan 2021 - Present · 3 yrs 9 mos", "Johannesburg"],
        ["Beta Integrators", "Full-time · 6 yrs", "Project Manager", "Mar 2019 - Dec 2020 · 1 yr 10 mos",
         "Engineer", "2015 - 2019 · 4 yrs"],
        ["Volunteer", "no dates here"],
        ["Technician", "Gamma", "2010 – 2015"],
    ]
    out = parse_experience(entries, limit=5)
    assert out[0] == {"title": "Head of Security", "company": "Acme Security", "dates": "Jan 2021 - Present"}
    assert out[1]["company"] == "Beta Integrators" and out[1]["title"] == "Project Manager"
    assert out[1]["dates"] == "Mar 2019 - Dec 2020"
    assert out[2] == {"title": "Technician", "company": "Gamma", "dates": "2010 – 2015"}
    assert len(parse_experience(entries, limit=1)) == 1


def test_summary_and_stop_detection():
    s = build_summary("CEO at Acme", "word " * 300, 120)
    assert s.startswith("CEO at Acme — word") and len(s) <= 120 and s.endswith("…")
    assert build_summary(None, "  ") is None
    assert is_stop_page("https://www.linkedin.com/checkpoint/challenge", "")
    assert is_stop_page("https://www.linkedin.com/in/x", "You've reached the commercial use limit on search")
    assert is_stop_page("https://www.linkedin.com/in/x", "Experience ...") is None


class FakeDriver:
    def __init__(self, script):
        self.script, self.seen, self.closed = script, [], False

    def fetch(self, contact, need_face):
        self.seen.append((contact["full_name"], need_face))
        r = self.script.get(contact["full_name"], Result("no_profile"))
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


def good(name, photo=True):
    return Result("done", profile_url=f"https://www.linkedin.com/in/{name.split()[0].lower()}", profile_name=name,
                  headline="Buyer at Acme Security", about="Procurement lead.",
                  experience=[{"title": "Senior Buyer", "company": "Acme Security", "dates": "2020 - Present"}],
                  photo=jpeg() if photo else None)


def test_run_enriches_saves_face_and_resumes(cfg, loaded):
    script = {"Bob Jones": good("Bob Jones"), "Carla Müller": good("Carla Muller", photo=False),
              "Leandro da Cunha": good("Someone Else")}
    d = FakeDriver(script)
    stats = harvest.run(loaded, cfg, lambda: d, sleep=lambda s: None, log=lambda *a: None)
    assert d.closed and stats["processed"] == 6
    bob = db.one(loaded, "SELECT * FROM contacts WHERE full_name='Bob Jones'")
    assert bob["enrich_status"] == "done" and bob["image_status"] == "downloaded"
    assert bob["image_filename"] == "CUST__Bob_Jones.jpg" and (cfg.kyc_folder / "CUST__Bob_Jones.jpg").exists()
    assert bob["linkedin_summary"] == "Buyer at Acme Security — Procurement lead."
    assert json.loads(bob["linkedin_experience"])[0]["title"] == "Senior Buyer"
    assert bob["role"] == "Buyer"  # source role kept (not overwritten by LinkedIn)
    assert json.loads(bob["extra"])["match_confidence"] == "name+company"
    carla = db.one(loaded, "SELECT * FROM contacts WHERE full_name='Carla Müller'")
    assert carla["enrich_status"] == "done" and carla["image_status"] == "downloaded"  # face already in library
    # wrong person on top match -> no_profile, nothing saved
    leandro = db.one(loaded, "SELECT * FROM contacts WHERE full_name='Leandro da Cunha'")
    assert leandro["enrich_status"] == "no_profile" and leandro["linkedin_summary"] is None
    # contacts that already had a face were not asked for one
    assert ("Anna Smith", False) in d.seen and ("Bob Jones", True) in d.seen
    # resumable: second run has nothing left
    d2 = FakeDriver({})
    assert harvest.run(loaded, cfg, lambda: d2, sleep=lambda s: None, log=lambda *a: None) == {"processed": 0}


def test_daily_cap_and_stop(cfg, loaded):
    d = FakeDriver({})
    stats = harvest.run(loaded, cfg, lambda: d, cap=2, sleep=lambda s: None, log=lambda *a: None)
    assert stats["processed"] == 2 and harvest.used_today(loaded) == 2
    stats = harvest.run(loaded, cfg, lambda: FakeDriver({}), cap=2, sleep=lambda s: None, log=lambda *a: None)
    assert stats["stopped"] == "daily cap"

    loaded.execute("DELETE FROM harvest_log")
    loaded.commit()
    first = harvest.queue(loaded, cfg)[0]["full_name"]
    d = FakeDriver({first: StopHarvest("checkpoint url")})
    stats = harvest.run(loaded, cfg, lambda: d, sleep=lambda s: None, log=lambda *a: None)
    assert stats["stopped"] == "checkpoint url" and stats["processed"] == 0 and d.closed
    assert db.one(loaded, "SELECT enrich_status FROM contacts WHERE full_name=?", [first])["enrich_status"] == "pending"


def test_failures_retry_then_give_up(cfg, loaded):
    name = "Bob Jones"
    ids = [db.one(loaded, "SELECT id FROM contacts WHERE full_name=?", [name])["id"]]
    for _ in range(2):
        harvest.run(loaded, cfg, lambda: FakeDriver({name: RuntimeError("boom")}), ids=ids, cap=99,
                    sleep=lambda s: None, log=lambda *a: None)
    bob = db.one(loaded, "SELECT * FROM contacts WHERE full_name=?", [name])
    assert bob["enrich_status"] == "failed" and bob["enrich_attempts"] == 2 and "boom" in bob["enrich_error"]
    # still needs a face, so it stays queued for the face only if attempts allow -> max_attempts=2 reached
    assert all(r["full_name"] != name or r["image_status"] == "none" for r in harvest.queue(loaded, cfg))


def test_login_failure_is_a_clean_stop(cfg, loaded):
    def factory():
        raise StopHarvest("login cancelled")
    stats = harvest.run(loaded, cfg, factory, sleep=lambda s: None, log=lambda *a: None)
    assert stats == {"processed": 0, "stopped": "login cancelled"}


class RedirectingPage:
    """Mimics the real failure: goto() interrupted by LinkedIn's redirect to a security check."""

    def __init__(self):
        self.url, self.waited = "about:blank", False

    def goto(self, url, **kw):
        self.url = "https://www.linkedin.com/checkpoint/challenge/AQF"
        raise RuntimeError('Page.goto: Navigation to "https://www.linkedin.com/feed/" is interrupted by another '
                           'navigation to "https://www.linkedin.com/checkpoint/challenge/AQF"')

    def wait_for_load_state(self, *a, **kw):
        self.waited = True


def test_redirect_to_security_check_waits_for_the_human(monkeypatch):
    monkeypatch.setattr(harvest.time, "sleep", lambda s: None)
    page = RedirectingPage()
    harvest.navigate(page, harvest.FEED_URL)  # used to crash here
    assert page.waited and harvest.on_sign_in_page(page.url)

    page2 = RedirectingPage()
    prompts = []

    def human(msg):  # finishes the check in the browser, which lands on the feed
        prompts.append(msg)
        page2.url = "https://www.linkedin.com/feed/"
        return ""

    harvest.ensure_login(page2, prompt=human)
    assert page2.url.endswith("/feed/") and len(prompts) == 1 and "security check" in prompts[0]

    with pytest.raises(harvest.StopHarvest, match="cancelled"):
        harvest.ensure_login(RedirectingPage(), prompt=lambda m: "q")

    class Other(RedirectingPage):
        def goto(self, url, **kw):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(RuntimeError):
        harvest.navigate(Other(), harvest.FEED_URL)
