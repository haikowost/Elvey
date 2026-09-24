"""LinkedIn enrichment: face + short profile summary + recent work history.

    python -m src.harvest --test          # 5 contacts, then stop
    python -m src.harvest --limit 20
    python -m src.harvest --dry-run       # show the queue, open nothing

Owner-run and interactive by design: it drives a *visible* Chromium window with a persistent
profile (data/chrome-profile) that you log into once by hand. No credentials are stored or
typed. Daily cap (default 40), randomised 4–9 s pacing, and it stops immediately on any
LinkedIn checkpoint / "commercial use limit" page. Every contact is checkpointed in the DB,
so re-runs skip completed work. Misses (no profile / no photo) are recorded, not retried.

Please stay within LinkedIn's User Agreement: modest volumes, your own account, for your
own KYC purposes.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from . import db
from .config import load_config
from .images import update_account_kyc_status
from .util import face_filename, norm_company, norm_name

try:
    from rapidfuzz import fuzz

    def name_score(a: str, b: str) -> float:
        return fuzz.token_set_ratio(norm_name(a), norm_name(b))
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def name_score(a: str, b: str) -> float:
        return 100 * SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()

NAME_MATCH_MIN = 80
STOP_MARKERS = ("commercial use limit", "security verification", "let's do a quick security check",
                "you've reached the weekly", "unusual activity", "your account has been restricted")
DATE_RE = re.compile(r"((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d{2}\s*[-–—]\s*"
                     r"(((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d{2}|present)",
                     re.IGNORECASE)
DURATION_RE = re.compile(r"^\s*(\d+\s*(yrs?|years?|mos?|months?)\s*)+$", re.IGNORECASE)
EMPLOYMENT_RE = re.compile(r"\b(full-time|part-time|self-employed|freelance|contract|internship|apprenticeship|"
                           r"seasonal)\b", re.IGNORECASE)


class StopHarvest(Exception):
    """LinkedIn showed a checkpoint / rate limit — stop the whole run."""


@dataclass
class Result:
    status: str                      # done | no_profile | failed
    profile_url: str | None = None
    profile_name: str | None = None
    headline: str | None = None
    about: str | None = None
    experience: list[dict] = field(default_factory=list)
    photo: bytes | None = None       # JPEG bytes, already downscaled
    error: str | None = None


class Driver(Protocol):
    def fetch(self, contact: dict, need_face: bool) -> Result: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------- parsing (pure)

def parse_experience(entries: list[list[str]], limit: int = 5) -> list[dict]:
    """Turn the text lines of each Experience <li> into {title, company, dates}.

    Handles single roles ([title, 'Company · Full-time', 'Jan 2020 - Present · 4 yrs', location])
    and grouped roles ([company, 'Full-time · 6 yrs', title, 'Mar 2021 - Present · 3 yrs', ...]).
    """
    out: list[dict] = []
    for lines in entries:
        lines = [l.strip() for l in lines if l and l.strip()]
        lines = list(dict.fromkeys(lines))  # LinkedIn repeats visually-hidden copies
        if not lines:
            continue
        date_idx = [i for i, l in enumerate(lines) if DATE_RE.search(l)]
        if not date_idx:
            continue
        grouped = len(lines) > 1 and (DURATION_RE.match(lines[1].split("·")[-1]) or
                                      (EMPLOYMENT_RE.search(lines[1]) and date_idx[0] > 2))
        if grouped:
            company = lines[0]
            i = date_idx[0]
            title = lines[i - 1] if i - 1 > 1 else lines[0]
        else:
            i = date_idx[0]
            title = lines[0]
            company = lines[1] if i > 1 else None
        if company:
            company = EMPLOYMENT_RE.sub("", company.split(" · ")[0]).strip(" ·")
        dates = DATE_RE.search(lines[i]).group(0)
        out.append({"title": title, "company": company or None, "dates": dates})
        if len(out) >= limit:
            break
    return out


def build_summary(headline: str | None, about: str | None, limit: int = 500) -> str | None:
    parts = [p.strip() for p in (headline, about) if p and p.strip()]
    if not parts:
        return None
    text = " — ".join(parts)
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def current_title(experience: list[dict], headline: str | None) -> str | None:
    for e in experience:
        if re.search(r"present", e.get("dates") or "", re.IGNORECASE):
            return e["title"]
    return (headline or "").split(" at ")[0].split("|")[0].strip() or None


def is_stop_page(url: str, body: str) -> str | None:
    u = url.lower()
    if "/checkpoint/" in u or "/authwall" in u:
        return f"checkpoint url: {url}"
    b = body.lower()
    for marker in STOP_MARKERS:
        if marker in b:
            return f"page says: {marker}"
    return None


def downscale_jpeg(data: bytes, max_px: int, quality: float) -> bytes:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality * 100), optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- Playwright driver

PHOTO_SELECTOR = ('img[src*="profile-displayphoto"], img[src*="profile-framedphoto"], '
                  'img[class*="profile-displayphoto"], img[class*="profile-framedphoto"], '
                  'img.pv-top-card-profile-picture__image--show, img.pv-top-card-profile-picture__image')

JS_CAPTURE = """
async ([root, sel, maxPx, q]) => {
  const scope = root ? document.querySelector(root) : (document.querySelector('main') || document.body);
  if (!scope) return null;
  const img = scope.querySelector(sel);
  if (!img) return null;
  if (!img.complete) await new Promise(r => { img.onload = r; img.onerror = r; setTimeout(r, 3000); });
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h || /ghost|static\\.licdn\\.com\\/aero/.test(img.src)) return null;
  const s = Math.min(1, maxPx / Math.max(w, h));
  const c = document.createElement('canvas');
  c.width = Math.round(w * s); c.height = Math.round(h * s);
  c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
  try { return {dataUrl: c.toDataURL('image/jpeg', q), src: img.src}; }
  catch (e) { return {src: img.src, tainted: true}; }
}
"""

JS_TOP_RESULT = """
(lastName) => {
  const main = document.querySelector('main') || document.body;
  const links = [...main.querySelectorAll('a[href*="/in/"]')]
    .filter(a => !a.closest('header') && /linkedin\\.com\\/in\\//.test(a.href));
  if (!links.length) return null;
  const want = (lastName || '').toLowerCase();
  const pick = links.find(a => want && a.innerText.toLowerCase().includes(want)) || links[0];
  const li = pick.closest('li');
  if (li) li.setAttribute('data-kyc-top', '1');
  return pick.href.split('?')[0];
}
"""

JS_PROFILE = """
() => {
  const main = document.querySelector('main');
  if (!main) return null;
  const txt = el => el ? el.innerText.trim() : '';
  const section = id => { const a = main.querySelector('#' + id); return a ? a.closest('section') : null; };
  const spans = el => [...el.querySelectorAll('span[aria-hidden="true"]')].map(s => s.innerText.trim()).filter(Boolean);
  const name = txt(main.querySelector('h1'));
  const headline = txt(main.querySelector('.text-body-medium'));
  let about = '';
  const a = section('about');
  if (a) { const s = spans(a); about = (s.length > 1 ? s.slice(1).join(' ') : txt(a).replace(/^About\\s*/, '')); }
  const e = section('experience');
  let experience = [];
  if (e) {
    const items = [...e.querySelectorAll('li')].filter(li => {
      const outer = li.parentElement && li.parentElement.closest('li');
      return !outer || !e.contains(outer);
    });
    experience = items.map(li => spans(li)).filter(x => x.length);
  }
  return {name, headline, about, experience};
}
"""


FEED_URL = "https://www.linkedin.com/feed/"
SIGN_IN_MARKERS = ("/login", "/uas/", "/authwall", "/signup", "/checkpoint/", "/challenge")


def navigate(page, url: str) -> None:
    """page.goto that tolerates LinkedIn redirecting mid-load (e.g. to a security check)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:  # playwright Error: 'interrupted by another navigation'
        if "interrupted by another navigation" not in str(e):
            raise
        page.wait_for_load_state("domcontentloaded", timeout=45000)


def on_sign_in_page(url: str) -> bool:
    return any(m in url for m in SIGN_IN_MARKERS)


def ensure_login(page, prompt=input, interactive: bool = True, max_rounds: int = 10) -> None:
    """Make sure the persistent profile is logged in. The human does the log-in and any LinkedIn
    security check in the browser window; this only waits and never navigates away from them."""
    navigate(page, FEED_URL)
    time.sleep(2)
    for _ in range(max_rounds):
        if "/feed" in page.url and not on_sign_in_page(page.url):
            return
        if not interactive:
            raise StopHarvest("not logged in to LinkedIn — run `python -m src.harvest --test` in a terminal once and log in")
        if on_sign_in_page(page.url):
            msg = ("\n>>> In the browser window: log into LinkedIn and complete any security check\n"
                   "    (code by email/SMS, puzzle, etc.). When you can see your LinkedIn feed, press Enter here\n"
                   "    (or type q + Enter to quit)… ")
        else:
            msg = "\n>>> Press Enter once you can see your LinkedIn feed in the browser window (q to quit)… "
        if prompt(msg).strip().lower() == "q":
            raise StopHarvest("login cancelled")
        time.sleep(1)
        if not on_sign_in_page(page.url) and "/feed" not in page.url:
            navigate(page, FEED_URL)  # logged in but somewhere else: go to the feed to confirm
            time.sleep(2)
        elif on_sign_in_page(page.url):
            print("    Still on a LinkedIn sign-in / security page — finish it in the browser first.")
    raise StopHarvest("still not logged in to LinkedIn")


class LinkedInDriver:
    def __init__(self, cfg, headless: bool = False):
        from playwright.sync_api import sync_playwright

        self.h = cfg.get("harvest") or {}
        profile = cfg.path("paths", "browser_profile") or cfg.data_dir / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch = {"headless": headless, "viewport": {"width": 1280, "height": 900}}
        if self.h.get("browser_channel"):
            launch["channel"] = self.h["browser_channel"]
        if self.h.get("executable_path"):
            launch["executable_path"] = self.h["executable_path"]
        self.ctx = self._pw.chromium.launch_persistent_context(str(profile), **launch)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        try:
            self._ensure_login()
        except StopHarvest:
            self.close()
            raise
        except Exception as e:  # e.g. the browser window was closed during login
            self.close()
            raise StopHarvest(f"browser problem during LinkedIn login: {str(e).splitlines()[0][:200]}") from e

    def _wait(self, lo: float = 1.5, hi: float = 3.0) -> None:
        time.sleep(random.uniform(lo, hi))

    def _nav(self, url: str) -> None:
        navigate(self.page, url)

    def _goto(self, url: str) -> None:
        self._nav(url)
        time.sleep(float(self.h.get("render_wait_s", 3)))
        reason = is_stop_page(self.page.url, self.page.inner_text("body")[:20000])
        if reason:
            raise StopHarvest(reason)

    def _ensure_login(self, prompt=input, interactive: bool | None = None) -> None:
        interactive = sys.stdin.isatty() if interactive is None else interactive
        ensure_login(self.page, prompt, interactive)

    def _capture(self, root: str | None) -> bytes | None:
        px, q = int(self.h.get("image_px", 240)), float(self.h.get("jpeg_quality", 0.85))
        got = self.page.evaluate(JS_CAPTURE, [root, PHOTO_SELECTOR, px, q])
        if not got:
            return None
        if got.get("dataUrl", "").startswith("data:image/jpeg;base64,"):
            return base64.b64decode(got["dataUrl"].split(",", 1)[1])
        resp = self.ctx.request.get(got["src"])  # canvas tainted: fetch with the session cookies instead
        return downscale_jpeg(resp.body(), px, q) if resp.ok else None

    def fetch(self, contact: dict, need_face: bool) -> Result:
        url = contact.get("linkedin_profile_url") or contact.get("linkedin_contact_url")
        search_photo = None
        if not url:
            q = f"{contact['full_name']} {contact.get('company') or ''}".strip()
            self._goto(f"https://www.linkedin.com/search/results/people/?keywords={quote(q)}")
            url = self.page.evaluate(JS_TOP_RESULT, (contact.get("last_name") or "").split(" ")[-1])
            if not url:
                return Result("no_profile")
            if need_face:  # photo inside the chosen result's own card (never another result's)
                search_photo = self._capture('li[data-kyc-top="1"]')
            self._wait()
        self._goto(url)
        for _ in range(4):  # Experience/About render lazily
            self.page.mouse.wheel(0, 1400)
            time.sleep(0.6)
        data = self.page.evaluate(JS_PROFILE) or {}
        if not data.get("name"):
            return Result("no_profile", profile_url=url)
        photo = None
        if need_face:
            self.page.mouse.wheel(0, -10000)
            photo = self._capture(None) or search_photo
        limit = int(self.h.get("max_experience", 5))
        return Result("done", profile_url=self.page.url.split("?")[0], profile_name=data["name"],
                      headline=data.get("headline"), about=data.get("about"),
                      experience=parse_experience(data.get("experience") or [], limit), photo=photo)

    def close(self) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()


# --------------------------------------------------------------------------- run loop (driver-agnostic)

def queue(conn, cfg, limit: int | None = None, ids: list[int] | None = None, segment: str | None = None) -> list[dict]:
    max_attempts = (cfg.get("harvest") or {}).get("max_attempts", 2)
    where = ["(c.image_status = 'none' OR c.enrich_status = 'pending' OR (c.enrich_status = 'failed' AND c.enrich_attempts < ?))",
             "c.enrich_status != 'no_profile'"]
    params: list = [max_attempts]
    if ids:
        where.append(f"c.id IN ({','.join('?' * len(ids))})")
        params += ids
    if segment:
        where.append("c.segment = ?")
        params.append(segment)
    sql = (f"SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id "
           f"WHERE {' AND '.join(where)} ORDER BY c.priority IS NULL, c.priority, c.id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.rows(conn, sql, params)


def used_today(conn) -> int:
    return conn.execute("SELECT count(*) FROM harvest_log WHERE day = ? AND result != 'stopped'",
                        [date.today().isoformat()]).fetchone()[0]


def apply_result(conn, cfg, contact: dict, res: Result, need_face: bool) -> str:
    """Write one harvest result to the DB (and the face to the KYC folder). Returns the log label."""
    h = cfg.get("harvest") or {}
    changes: dict = {"enrich_last_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    label = res.status
    if res.status == "done" and res.profile_name and name_score(res.profile_name, contact["full_name"]) < NAME_MATCH_MIN:
        res = Result("no_profile", error=f"top match was '{res.profile_name}'")
        label = "no_profile"
    extra = db.jload(contact.get("extra"), {})
    if res.status == "done":
        changes.update({
            "enrich_status": "done", "enrich_error": None,
            "linkedin_summary": build_summary(res.headline, res.about, int(h.get("summary_chars", 500))),
            "linkedin_experience": db.jdump(res.experience),
            "linkedin_profile_url": res.profile_url,
        })
        if not contact.get("linkedin_contact_url") and res.profile_url:
            changes["linkedin_contact_url"] = res.profile_url
        if not contact.get("role"):
            role = current_title(res.experience, res.headline)
            if role:
                changes["role"] = role[:100]
                extra["role_status"] = "from LinkedIn"
        company = norm_company(contact.get("company"))
        blob = norm_company(" ".join([res.headline or ""] + [e.get("company") or "" for e in res.experience]))
        extra["match_confidence"] = "name+company" if company and company in blob else "name-only"
        changes["extra"] = db.jdump(extra)
        if need_face:
            if res.photo:
                fname = face_filename(contact["segment"], contact["full_name"], contact.get("company"), cfg)
                folder = cfg.kyc_folder
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / fname
                if not target.exists():
                    target.write_bytes(res.photo)
                changes.update({"image_filename": fname, "image_status": "downloaded"})
            else:
                changes["image_status"] = "no_photo"
                label = "done/no_photo"
    elif res.status == "no_profile":
        changes.update({"enrich_status": "no_profile", "enrich_error": res.error})
        if need_face:
            changes["image_status"] = "no_photo"
    else:
        changes.update({"enrich_status": "failed", "enrich_error": (res.error or "")[:500],
                        "enrich_attempts": (contact.get("enrich_attempts") or 0) + 1})
    with conn:
        db.update(conn, "contacts", contact["id"], changes)
        conn.execute("INSERT INTO harvest_log(contact_id, day, result, detail) VALUES (?,?,?,?)",
                     [contact["id"], date.today().isoformat(), label, res.error or res.profile_url])
    return label


def run(conn, cfg, driver_factory, limit: int | None = None, cap: int | None = None, ids=None, segment=None,
        sleep=time.sleep, log=print) -> dict:
    h = cfg.get("harvest") or {}
    cap = int(cap if cap is not None else h.get("daily_cap", 40))
    remaining = cap - used_today(conn)
    if remaining <= 0:
        log(f"Daily cap of {cap} reached — come back tomorrow (or pass --cap).")
        return {"processed": 0, "stopped": "daily cap"}
    todo = queue(conn, cfg, min(limit or remaining, remaining), ids, segment)
    if not todo:
        log("Nothing to enrich.")
        return {"processed": 0}
    stats: dict = {"processed": 0}
    try:
        driver = driver_factory()
    except StopHarvest as e:
        log(f"STOPPED before starting: {e}")
        return {**stats, "stopped": str(e)}
    try:
        for n, c in enumerate(todo, 1):
            need_face = c["image_status"] == "none"
            log(f"[{n}/{len(todo)}] {c['full_name']} ({c.get('company') or '-'}) face={'y' if need_face else 'n'}")
            try:
                res = driver.fetch(c, need_face)
            except StopHarvest as e:
                with conn:
                    conn.execute("INSERT INTO harvest_log(contact_id, day, result, detail) VALUES (?,?,?,?)",
                                 [c["id"], date.today().isoformat(), "stopped", str(e)])
                log(f"STOPPED: {e}")
                stats["stopped"] = str(e)
                break
            except Exception as e:  # one bad page must not kill the run
                res = Result("failed", error=f"{type(e).__name__}: {e}")
            label = apply_result(conn, cfg, c, res, need_face)
            stats[label] = stats.get(label, 0) + 1
            stats["processed"] += 1
            log(f"    -> {label}")
            if n < len(todo):
                sleep(random.uniform(float(h.get("delay_min_s", 4)), float(h.get("delay_max_s", 9))))
    finally:
        driver.close()
        with conn:
            update_account_kyc_status(conn)
    stats["used_today"] = used_today(conn)
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="max contacts this run")
    ap.add_argument("--test", action="store_true", help="5-contact test run")
    ap.add_argument("--cap", type=int, help="override harvest.daily_cap for today")
    ap.add_argument("--ids", help="comma-separated contact ids")
    ap.add_argument("--segment", choices=["customer", "competitor", "internal"])
    ap.add_argument("--dry-run", action="store_true", help="print the queue only")
    ap.add_argument("--headless", action="store_true", help="not recommended (LinkedIn is stricter)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    limit = 5 if args.test else args.limit
    if args.dry_run:
        rows = queue(conn, cfg, limit or 50, ids, args.segment)
        for r in rows:
            print(f"{r['id']:>5}  p={r['priority']}  {r['segment']:<10} {r['full_name']:<30} {r.get('company') or ''}"
                  f"  face={'need' if r['image_status'] == 'none' else r['image_status']}  summary={r['enrich_status']}")
        print(f"\n{len(rows)} shown · used today: {used_today(conn)}/{(cfg.get('harvest') or {}).get('daily_cap', 40)}")
        return
    stats = run(conn, cfg, lambda: LinkedInDriver(cfg, headless=args.headless), limit, args.cap, ids, args.segment)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
