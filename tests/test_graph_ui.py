"""The /graph front-end's acceptance criteria (build brief §9), driven in real Chromium against the
demo seed. Skipped when Playwright / a Chromium build / the built bundle is unavailable."""
import os
import socket
import threading
import time

import pytest

from src import dashboard

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
uvicorn = pytest.importorskip("uvicorn")
pytestmark = pytest.mark.skipif(not (dashboard.GRAPH_DIST / "index.html").exists(), reason="front-end not built")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    from src.config import load_config

    tmp = tmp_path_factory.mktemp("graphui")
    os.environ["ELVEY_GRAPH_SOURCE"] = "seed"
    cfg = load_config(overrides={"paths.db": str(tmp / "t.db"), "paths.kyc_folder": str(tmp / "kyc"),
                                 "paths.data_dir": str(tmp / "data")})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(dashboard.create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/graph/"
    server.should_exit = True
    t.join(5)
    os.environ.pop("ELVEY_GRAPH_SOURCE", None)


@pytest.fixture(scope="module")
def browser():
    exe = os.environ.get("KYC_TEST_CHROMIUM") or next(
        (p for p in ("/opt/pw-browsers/chromium", "/usr/bin/chromium") if os.path.exists(p)), None)
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(**({"executable_path": exe} if exe and os.path.isfile(exe) else {}),
                                   args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        except Exception as e:  # pragma: no cover
            pytest.skip(f"no chromium: {e}")
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    yield p
    p.close()
    assert not errors, errors  # no uncaught exception in any scenario


def _view(page, name):
    page.get_by_role("radio", name=name).click()


def test_1_investigate_veracitech(page, base_url):
    page.goto(base_url)
    page.get_by_label("Account picker").select_option(label="Veracitech")
    inv = page.get_by_test_id("investigate")
    inv.wait_for()
    contacts = inv.get_by_role("region", name="Contacts & reps")
    for name in ("JP Nel", "Thabo Mokoena", "Riaan Botha"):
        expect(contacts.get_by_text(name, exact=True)).to_be_visible()
    expect(inv.get_by_role("region", name="Linked consultants / specifiers").get_by_text("Consult-Eng Partners", exact=True)).to_be_visible()
    endusers = inv.get_by_role("region", name="Linked end-users")
    for name in ("Barloworld", "Transnet", "Waterfall Estate"):
        expect(endusers.get_by_text(name, exact=True)).to_be_visible()
    assert inv.get_by_role("region", name="Opportunities").locator(".opp-row").count() >= 3
    assert page.evaluate("location.hash") == "#investigate:veracitech"  # deep link


def test_2_spider_reroot_and_breadcrumb(page, base_url):
    page.goto(base_url + "#spider:veracitech")
    spider = page.get_by_test_id("spider")
    spider.get_by_role("button", name="Re-centre on Barloworld", exact=True).click()
    page.wait_for_function("location.hash === '#spider:barloworld'")
    expect(spider.get_by_role("button", name="Barloworld details")).to_be_visible()  # it's the centre now
    crumbs = page.get_by_role("navigation", name="Breadcrumbs")
    expect(crumbs.get_by_role("button", name="Veracitech")).to_be_visible()
    crumbs.get_by_role("button", name="Back").click()
    page.wait_for_function("location.hash === '#spider:veracitech'")


def test_3_org_expands_elvey_and_veracitech(page, base_url):
    page.goto(base_url + "#org")
    org = page.get_by_test_id("org")
    org.get_by_role("button", name="Elvey", exact=True).click()
    for name in ("Pentagon Distribution", "Quin", "Lerato Dlamini"):
        expect(org.get_by_role("button", name=name, exact=True)).to_be_visible()
    org.get_by_role("button", name="Veracitech", exact=True).click()
    for name in ("JP Nel", "Thabo Mokoena", "Riaan Botha", "Transnet ANPR", "Barloworld Tender"):
        expect(org.get_by_role("button", name=name, exact=True).first).to_be_visible()


def test_4_selection_survives_view_switches(page, base_url):
    page.goto(base_url + "#graph:veracitech")
    page.wait_for_function("location.hash === '#graph:veracitech'")
    _view(page, "Spider")
    expect(page.get_by_test_id("spider").get_by_role("button", name="Veracitech details")).to_be_visible()
    _view(page, "Org")
    page.wait_for_selector('.ocard.selected[data-id="veracitech"]')
    _view(page, "Investigate")
    expect(page.get_by_test_id("investigate").get_by_role("heading", name="Veracitech")).to_be_visible()
    assert page.evaluate("location.hash") == "#investigate:veracitech"


def test_5_hover_insight_card_with_zoho_chip(page, base_url):
    page.goto(base_url + "#spider:veracitech")
    page.get_by_test_id("spider").get_by_role("button", name="Re-centre on JP Nel").hover()
    card = page.get_by_role("dialog", name="JP Nel insight")
    card.wait_for()
    assert card.locator(".zchip.in").inner_text() == "in Zoho"
    page.get_by_test_id("spider").get_by_role("button", name="Re-centre on Barloworld", exact=True).hover()
    card = page.get_by_role("dialog", name="Barloworld insight")
    card.wait_for()
    assert card.locator(".zchip.out").inner_text() == "not synced"


def test_6_unenriched_contact_shows_kyc_pending(page, base_url):
    page.goto(base_url + "#spider:veracitech")
    page.get_by_role("button", name="Re-centre on Riaan Botha").hover()
    page.get_by_role("dialog", name="Riaan Botha insight").get_by_role("button", name="Details").click()
    dossier = page.get_by_role("complementary", name="Riaan Botha dossier")
    dossier.wait_for()
    assert "KYC pending" in dossier.inner_text()


def test_graph_view_renders_and_lens_toggles(page, base_url):
    page.goto(base_url)
    page.get_by_test_id("graph3d").locator("canvas").wait_for()
    page.get_by_label("Lenses").get_by_role("button", name="Projects").click()
    expect(page.get_by_label("Lenses").get_by_role("button", name="reset")).to_be_visible()
    page.keyboard.press("Control+k")
    page.get_by_label("Search entities").fill("transn")
    page.get_by_role("dialog", name="Search").get_by_role("button").first.click()
    page.wait_for_function("location.hash.startsWith('#graph:transnet')")
