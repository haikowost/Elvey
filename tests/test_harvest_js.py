"""Runs the harvester's in-page JavaScript against mock LinkedIn markup in real Chromium.
Skipped when Playwright / a Chromium build is not available."""
import base64
import io
import os

import pytest
from PIL import Image

from src import harvest

sync_api = pytest.importorskip("playwright.sync_api")


def _img_data_url(w=400, h=400):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


SEARCH = """<header><a href="https://www.linkedin.com/in/me/"><img class="global-nav__me-photo profile-displayphoto" src="{img}"></a></header>
<main><ul>
  <li><a href="https://www.linkedin.com/in/other-person/?mini=1">Other Person</a><img class="x" src="{img}"></li>
  <li><a href="https://www.linkedin.com/in/bob-jones-123/?miniProfileUrn=x"><span>Bob Jones</span></a>
      <img class="presence-entity__image EntityPhoto-circle-3 profile-displayphoto" src="{img}"></li>
</ul></main>"""

PROFILE = """<main>
 <section><img class="pv-top-card-profile-picture__image--show" src="{img}"><h1>Bob Jones</h1>
   <div class="text-body-medium">Senior Buyer at Acme Security</div></section>
 <section><div id="about"></div><span aria-hidden="true">About</span><span aria-hidden="true">Procurement lead for security projects.</span></section>
 <section><div id="experience"></div><span aria-hidden="true">Experience</span><ul>
   <li><span aria-hidden="true">Senior Buyer</span><span aria-hidden="true">Acme Security · Full-time</span>
       <span aria-hidden="true">Jan 2021 - Present · 3 yrs</span></li>
   <li><span aria-hidden="true">Beta Integrators</span><span aria-hidden="true">Full-time · 6 yrs</span>
       <ul><li><span aria-hidden="true">Project Manager</span><span aria-hidden="true">Mar 2019 - Dec 2020 · 1 yr</span></li></ul></li>
 </ul></section>
</main>"""


@pytest.fixture(scope="module")
def page():
    exe = os.environ.get("KYC_TEST_CHROMIUM") or next(
        (p for p in ("/opt/pw-browsers/chromium", "/usr/bin/chromium") if os.path.exists(p)), None)
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**({"executable_path": exe} if exe and os.path.isfile(exe) else {}))
        except Exception as e:  # pragma: no cover
            pytest.skip(f"no chromium: {e}")
        yield browser.new_page()
        browser.close()


def test_search_top_result_and_card_photo(page):
    page.set_content(SEARCH.format(img=_img_data_url()))
    url = page.evaluate(harvest.JS_TOP_RESULT, "Jones")
    assert url == "https://www.linkedin.com/in/bob-jones-123/"
    got = page.evaluate(harvest.JS_CAPTURE, ['li[data-kyc-top="1"]', harvest.PHOTO_SELECTOR, 240, 0.85])
    raw = base64.b64decode(got["dataUrl"].split(",", 1)[1])
    assert Image.open(io.BytesIO(raw)).size == (240, 240)


def test_profile_extraction(page):
    page.set_content(PROFILE.format(img=_img_data_url(800, 600)))
    data = page.evaluate(harvest.JS_PROFILE)
    assert data["name"] == "Bob Jones" and data["headline"] == "Senior Buyer at Acme Security"
    assert data["about"] == "Procurement lead for security projects."
    exp = harvest.parse_experience(data["experience"])
    assert exp[0] == {"title": "Senior Buyer", "company": "Acme Security", "dates": "Jan 2021 - Present"}
    assert exp[1]["company"] == "Beta Integrators" and exp[1]["title"] == "Project Manager"
    got = page.evaluate(harvest.JS_CAPTURE, [None, harvest.PHOTO_SELECTOR, 240, 0.85])
    raw = base64.b64decode(got["dataUrl"].split(",", 1)[1])
    assert max(Image.open(io.BytesIO(raw)).size) == 240


def test_no_photo_returns_none(page):
    page.set_content("<main><h1>Nobody</h1><img class='ghost-person' src='https://static.licdn.com/aero/ghost.svg'></main>")
    assert page.evaluate(harvest.JS_CAPTURE, [None, harvest.PHOTO_SELECTOR, 240, 0.85]) is None


def test_login_waits_through_security_check(page):
    """Reproduces LinkedIn redirecting the feed to /checkpoint/ mid-load after a fresh log-in."""
    state = {"verified": False}

    def handler(route):
        url = route.request.url
        if "/feed" in url and not state["verified"]:
            route.fulfill(content_type="text/html",
                          body="<script>location.replace('https://www.linkedin.com/checkpoint/challenge/abc')</script>")
        elif "/feed" in url:
            route.fulfill(content_type="text/html", body="<main>Your feed</main>")
        else:
            route.fulfill(content_type="text/html", body="<main>Let's do a quick security check</main>")

    page.route("https://www.linkedin.com/**", handler)
    prompts = []

    def human(msg):  # the person finishes the check in the window, which lands on the feed
        prompts.append(msg)
        state["verified"] = True
        harvest.navigate(page, harvest.FEED_URL)
        return ""

    harvest.ensure_login(page, prompt=human, interactive=True)
    assert "/feed" in page.url and len(prompts) == 1 and "security check" in prompts[0]
    page.unroute("https://www.linkedin.com/**")


def test_login_not_interactive_stops_cleanly(page):
    page.route("https://www.linkedin.com/**", lambda r: r.fulfill(
        content_type="text/html", body="<script>location.replace('https://www.linkedin.com/login')</script>"
        if "/feed" in r.request.url else "<main>Sign in</main>"))
    with pytest.raises(harvest.StopHarvest, match="not logged in"):
        harvest.ensure_login(page, interactive=False)
    with pytest.raises(harvest.StopHarvest, match="cancelled"):
        harvest.ensure_login(page, prompt=lambda m: "q", interactive=True)
    page.unroute("https://www.linkedin.com/**")


# Newer LinkedIn layout: hashed class names, no #about / #experience anchors, no .text-body-medium,
# visually-hidden duplicates of every line.
def _dup(t):
    return f'<span aria-hidden="true">{t}</span><span class="visually-hidden">{t}</span>'


NEW_PROFILE = f"""<main>
 <section class="a1b2"><img class="c3d4 profile-displayphoto" src="{{img}}">
   <h1 class="x9">Bob Jones</h1><span class="q1">He/Him</span><span class="q2">· 2nd</span>
   <div class="z7">Senior Buyer at Acme Security | Procurement</div>
   <div>Johannesburg, Gauteng, South Africa</div><a>Contact info</a></section>
 <section class="k2"><h2 class="h">{_dup("About")}</h2>
   <div>{_dup("Procurement lead for security projects across SADC.")}<button>…see more</button></div></section>
 <section class="k3"><h2>{_dup("Activity")}</h2><p>312 followers</p></section>
 <section class="k4"><h2>{_dup("Experience")}</h2><ul>
   <li><div>{_dup("Senior Buyer")}</div><div>{_dup("Acme Security · Full-time")}</div>
       <div>{_dup("Jan 2021 - Present · 3 yrs 9 mos")}</div><div>{_dup("Johannesburg · On-site")}</div></li>
   <li><div>{_dup("Beta Integrators")}</div><div>{_dup("Full-time · 6 yrs")}</div><ul>
       <li><div>{_dup("Project Manager")}</div><div>{_dup("Mar 2019 - Dec 2020 · 1 yr 10 mos")}</div></li>
       <li><div>{_dup("Engineer")}</div><div>{_dup("2015 - 2019 · 4 yrs")}</div></li></ul></li>
 </ul><a>Show all 7 experiences</a></section>
 <section class="k5"><h2>{_dup("Education")}</h2><p>Wits · 2010 - 2014</p></section>
</main>"""


SR_ONLY_CSS = ("<style>.visually-hidden{position:absolute!important;width:1px;height:1px;overflow:hidden;"
               "clip:rect(1px,1px,1px,1px);white-space:nowrap}</style>")


@pytest.mark.parametrize("css", ["", SR_ONLY_CSS], ids=["unstyled", "linkedin-sr-only-css"])
def test_profile_extraction_new_layout(page, css):
    page.set_content(css + NEW_PROFILE.format(img=_img_data_url()))
    data = page.evaluate(harvest.JS_PROFILE)
    assert data["name"] == "Bob Jones" and not data["headline"] and not data["about"]  # old selectors miss
    headline, about, exp = harvest.parse_profile(data)
    assert headline == "Senior Buyer at Acme Security | Procurement"
    assert about == "Procurement lead for security projects across SADC."
    assert exp[0] == {"title": "Senior Buyer", "company": "Acme Security", "dates": "Jan 2021 - Present"}
    assert [e["title"] for e in exp[:3]] == ["Senior Buyer", "Project Manager", "Engineer"]
    assert exp[1]["company"] == exp[2]["company"] == "Beta Integrators"
    assert harvest.build_summary(headline, about).startswith("Senior Buyer at Acme Security")
