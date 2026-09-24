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
from .util import (DEPT_RANK, LOC_RANK, classify_department, face_filename, norm_company, norm_name, same_company,
                   split_location)

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
    location: str | None = None      # raw 'City, Province, Country' as shown on the profile
    photo: bytes | None = None       # JPEG bytes, already downscaled
    error: str | None = None
    debug_text: str | None = None    # visible page text (saved to data/debug when something went wrong)
    match_how: str | None = None     # how the profile was found: stored url | name+company | name-only …


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


SECTION_HEADINGS = {"about", "activity", "experience", "education", "skills", "featured", "interests",
                    "licenses & certifications", "volunteering", "recommendations", "languages", "projects",
                    "honors & awards", "courses", "organizations", "publications", "services", "highlights",
                    "people also viewed", "people you may know", "more profiles for you", "analytics", "resources",
                    "causes", "patents", "test scores", "contact info"}
_JUNK_LINE = re.compile(r"^(·\s*)?(1st|2nd|3rd\+?|3rd|he/him|she/her|they/them|he/they|she/they|verified|"
                        r"contact info|message|connect|follow|more|open to|\d+\+? (connections|followers))\b",
                        re.IGNORECASE)

# The page-root selector falls back to document.body when a profile has no <main> landmark yet
# (slow render, unusual layout). That pulls in LinkedIn's global header/footer nav, whose links
# ('About', 'Accessibility', 'Talent Solutions', 'Community Guidelines', ...) can otherwise be
# mistaken for the profile's own "About" section by the text-based fallback below. Anything from
# the first footer marker on is cut before any text-based parsing runs.
_FOOTER_RE = re.compile(r"\b(Accessibility|Talent Solutions|Community Guidelines|Ad Choices|Advertising|"
                        r"Sales Solutions|Safety Center|Cookie Policy|User Agreement|Marketing Solutions|"
                        r"LinkedIn Corporation|Select Language)\b", re.IGNORECASE)


def strip_footer(lines: list[str]) -> list[str]:
    for i, line in enumerate(lines):
        if _FOOTER_RE.search(line):
            return lines[:i]
    return lines


def clean_profile_data(data: dict) -> dict:
    """Drop anything from the page's footer/global nav before any text-based parsing runs."""
    return {**data, "topLines": strip_footer(data.get("topLines") or []),
            "text": strip_footer(data.get("text") or []),
            "sections": [s for s in (data.get("sections") or []) if not _FOOTER_RE.search(s.get("heading") or "")]}


def _heading_key(line: str) -> str:
    """'Experience' / 'Experience 4' / 'Experience4' / 'Experience ·' -> 'experience'."""
    key = (line or "").strip().lower()
    key = re.sub(r"[\s·:\-]*\d+[\s·:\-]*$", "", key)   # trailing count badge
    return key.strip(" ·:-")


def _is_heading(line: str) -> bool:
    return _heading_key(line) in SECTION_HEADINGS


def section_lines(data: dict, heading: str) -> list[str]:
    """Lines of the profile section titled `heading` (from <section> headings, else from the page text)."""
    for sec in data.get("sections") or []:
        if _heading_key(sec.get("heading", "")) == heading:
            return [l for l in sec.get("lines", [])[1:] if _heading_key(l) != heading]
    text = data.get("text") or []
    for i, l in enumerate(text):
        if _heading_key(l) == heading:
            out = []
            for m in text[i + 1:]:
                if _is_heading(m):
                    break
                out.append(m)
            return out
    return []


def headline_from(data: dict) -> str | None:
    if (data.get("headline") or "").strip():
        return data["headline"].strip()
    name = norm_name(data.get("name"))
    for l in data.get("topLines") or []:
        if norm_name(l) == name or _JUNK_LINE.match(l) or len(l) < 4 or l.startswith("·"):
            continue
        return l
    return None


def about_from(data: dict) -> str | None:
    if (data.get("about") or "").strip():
        text = data["about"]
    else:
        text = " ".join(section_lines(data, "about"))
    text = re.sub(r"\s*(…|\.\.\.)?\s*see more\s*$", "", text.strip(), flags=re.IGNORECASE)
    return text or None


def experience_from_lines(lines: list[str], limit: int = 5) -> list[dict]:
    """Experience from a flat list of visible lines (layout-independent fallback)."""
    lines = [l for l in lines if not re.match(r"^(show all|see all|…?see more)\b", l, re.IGNORECASE)]
    out, group, last = [], None, -1
    for i, l in enumerate(lines):
        m = DATE_RE.search(l)
        if not m:
            continue
        block = lines[last + 1:i]
        last = i
        if not block:
            continue
        if len(block) >= 3 and (DURATION_RE.match(block[-2].split("·")[-1]) or
                                (EMPLOYMENT_RE.search(block[-2]) and re.search(r"\d+\s*(yrs?|mos?)", block[-2]))):
            group = block[-3]                       # [Company, 'Full-time · 6 yrs', Title]
            title, company = block[-1], group
        elif len(block) >= 2 and (EMPLOYMENT_RE.search(block[-1]) or group is None):
            title, company, group = block[-2], block[-1], None   # [Title, 'Company · Full-time']
        elif group:
            title, company = block[-1], group       # next role inside the same company group
        else:
            title, company = block[-1], None
        company = EMPLOYMENT_RE.sub("", company.split(" · ")[0]).strip(" ·") if company else None
        out.append({"title": title, "company": company or None, "dates": m.group(0)})
        if len(out) >= limit:
            break
    return out


def parse_profile(data: dict, limit: int = 5) -> tuple[str | None, str | None, list[dict]]:
    """(headline, about, experience) from the page data, trying the structured read first."""
    data = clean_profile_data(data)
    experience = parse_experience(data.get("experience") or [], limit)
    if not experience:
        experience = experience_from_lines(section_lines(data, "experience"), limit)
    return headline_from(data), about_from(data), experience


def location_from(data: dict) -> str | None:
    """The top-card location line ('Johannesburg, Gauteng, South Africa'), if the page shows one."""
    data = clean_profile_data(data)
    headline = (headline_from(data) or "").strip()
    name = norm_name(data.get("name"))
    for l in data.get("topLines") or []:
        l = l.strip()
        if norm_name(l) == name or l == headline or _JUNK_LINE.match(l) or len(l) < 3:
            continue
        if "," in l and len(l) < 100 and not re.search(r"\bat\b", l, re.IGNORECASE):
            return l
    return None


def name_from_title(title: str | None) -> str | None:
    """'(3) Bob Jones | LinkedIn' -> 'Bob Jones'."""
    if not title or "linkedin" not in title.lower():
        return None
    name = re.sub(r"^\(\d+\)\s*", "", title).split(" | ")[0].split(" - ")[0].strip()
    return name if name and name.lower() != "linkedin" else None


def pick_candidate(cands: list[dict], contact: dict, company_in_query: bool) -> tuple[str | None, str]:
    """Choose the search result that is this person. Returns (profile url, how it was matched)."""
    first = norm_name(contact.get("first_name") or contact["full_name"].split()[0])
    last = norm_name(contact.get("last_name") or "").split()
    last = last[-1] if last else ""
    company = norm_company(contact.get("company"))
    co_token = next((t for t in company.split() if len(t) >= 3), company)
    named = []
    for c in cands:
        text = norm_name(c.get("text"))
        if first and first in text and (not last or last in text):
            named.append(c)
    if co_token:
        for c in named:
            if co_token in norm_company(c.get("text")):
                return c["href"], "name+company"
    if named and company_in_query:
        return named[0]["href"], "name+search"   # LinkedIn matched the company somewhere on the profile
    if len(named) == 1:
        return named[0]["href"], "name-only"
    return None, f"{len(named)} name match(es) among {len(cands)} result(s), none at {contact.get('company') or '?'}"


def current_company(experience: list[dict], headline: str | None) -> str | None:
    for e in experience:
        if re.search(r"present", e.get("dates") or "", re.IGNORECASE) and e.get("company"):
            return e["company"]
    m = re.search(r"\bat\s+(.+?)(\s*[|,•·]|$)", headline or "", re.IGNORECASE)
    return m.group(1).strip() if m else None


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

JS_RESULTS = """
() => {
  const root = document.querySelector('main') || document.body;
  const seen = new Set(), out = [];
  for (const a of root.querySelectorAll('a[href*="/in/"]')) {
    if (a.closest('header') || !/linkedin\\.com\\/in\\//.test(a.href)) continue;
    const href = a.href.split('?')[0];
    if (seen.has(href)) continue;
    seen.add(href);
    const card = a.closest('li') || a.parentElement;
    out.push({href, text: ((card && card.innerText) || a.innerText || '').slice(0, 600)});
    if (out.length >= 10) break;
  }
  return out;
}
"""

JS_MARK_RESULT = """
(href) => {
  const root = document.querySelector('main') || document.body;
  const a = [...root.querySelectorAll('a[href*="/in/"]')].find(x => x.href.split('?')[0] === href);
  const card = a && (a.closest('li') || a.parentElement);
  if (card) card.setAttribute('data-kyc-top', '1');
  return !!card;
}
"""

JS_PROFILE = """
() => {
  const main = document.querySelector('main') || document.body;
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
  // layout-independent fallbacks: section headings + visible text lines
  const lines = el => {
    const out = [];
    for (let l of (el ? el.innerText : '').split('\\n').map(x => x.trim()).filter(Boolean)) {
      l = l.replace(/\\s*(…|\\.\\.\\.)\\s*see more$/i, '');
      const h = l.length / 2;   // screen-reader copy glued on: 'AboutAbout' -> 'About'
      if (l.length > 1 && l.length % 2 === 0 && l.slice(0, h) === l.slice(h)) l = l.slice(0, h);
      if (l && out[out.length - 1] !== l) out.push(l);   // or repeated on its own line
    }
    return out;
  };
  // topLines used to be scoped to the <h1>'s own container, which misses the headline whenever
  // LinkedIn's real (hashed-class) markup puts it in a sibling branch instead. Read straight off
  // the page's first visible lines instead — order (name, headline, location, ...) is what the
  // headline/location fallbacks below actually rely on, not any particular DOM nesting.
  const sections = [...main.querySelectorAll('section')].map(sec => {
    const h = sec.querySelector('h2, h3, [role="heading"]');
    return {heading: h ? (lines(h)[0] || '') : '', lines: lines(sec).slice(0, 300)};
  }).filter(x => x.heading);
  return {name, headline, about, experience, topLines: lines(main).slice(0, 20), sections,
          text: lines(main).slice(0, 600), title: document.title};
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

    def _search(self, contact: dict) -> tuple[str | None, str, str]:
        """Search 'name company', then 'name'. Returns (url, how matched, debug text)."""
        name = contact["full_name"]
        company = norm_company(contact.get("company"))
        queries = [f"{name} {company}".strip()] + ([name] if company else [])
        debug = []
        for q in queries:
            self._goto(f"https://www.linkedin.com/search/results/people/?keywords={quote(q)}")
            cands = self.page.evaluate(JS_RESULTS) or []
            debug.append(f"search: {q}\n" + "\n---\n".join(c["text"] for c in cands) or "(no results)")
            url, how = pick_candidate(cands, contact, company_in_query=q != name)
            if url:
                return url, how, "\n".join(debug)
            self._wait()
        return None, how, "\n\n".join(debug)

    def fetch(self, contact: dict, need_face: bool) -> Result:
        url = contact.get("linkedin_profile_url") or contact.get("linkedin_contact_url")
        how, search_photo, search_debug = "stored url", None, ""
        if not url:
            url, how, search_debug = self._search(contact)
            if not url:
                return Result("no_profile", error=f"no matching LinkedIn search result ({how})", debug_text=search_debug)
            if need_face and self.page.evaluate(JS_MARK_RESULT, url):  # the chosen result's own photo only
                search_photo = self._capture('[data-kyc-top="1"]')
            self._wait()
        self._goto(url)
        try:
            self.page.wait_for_selector("h1", timeout=10000)
        except Exception:
            pass  # fall back to the tab title below
        for _ in range(4):  # Experience/About render lazily
            self.page.mouse.wheel(0, 1400)
            time.sleep(0.6)
        data = self.page.evaluate(JS_PROFILE) or {}
        page_text = f"url: {self.page.url}\ntitle: {data.get('title')}\n\n" + "\n".join(data.get("text") or [])
        name = (data.get("name") or "").strip() or name_from_title(data.get("title"))
        if not name:
            return Result("no_profile", profile_url=url, error="profile page showed no name",
                          debug_text=page_text + ("\n\n" + search_debug if search_debug else ""))
        photo = None
        if need_face:
            self.page.mouse.wheel(0, -10000)
            time.sleep(0.5)
            photo = self._capture(None) or search_photo
        headline, about, experience = parse_profile(data, int(self.h.get("max_experience", 5)))
        location = location_from(data)
        return Result("done", profile_url=self.page.url.split("?")[0], profile_name=name, headline=headline,
                      about=about, experience=experience, location=location, photo=photo, match_how=how,
                      debug_text=page_text)

    def close(self) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()


# --------------------------------------------------------------------------- run loop (driver-agnostic)

def queue(conn, cfg, limit: int | None = None, ids: list[int] | None = None, segment: str | None = None) -> list[dict]:
    max_attempts = (cfg.get("harvest") or {}).get("max_attempts", 2)
    where = ["(c.image_status = 'none' OR c.enrich_status = 'pending' OR (c.enrich_status = 'failed' AND c.enrich_attempts < ?))",
             "c.enrich_status != 'no_profile'", "c.contact_status = 'active'"]
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


def save_debug(cfg, contact: dict, text: str | None, kind: str) -> None:
    if not text:
        return
    dbg = cfg.data_dir / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    (dbg / f"{kind}_{contact['id']}.txt").write_text(f"contact: {contact['full_name']} ({contact.get('company')})\n"
                                                    f"{text}", encoding="utf-8")


def find_account(conn, company: str | None) -> int | None:
    """Existing account for a company name LinkedIn reported (exact normalised name first, then loose)."""
    if not company:
        return None
    exact = db.one(conn, "SELECT id FROM accounts WHERE name_norm = ?", [norm_company(company)])
    if exact:
        return exact["id"]
    for a in db.rows(conn, "SELECT id, name FROM accounts ORDER BY rank IS NULL, rank, id"):
        if same_company(a["name"], company):
            return a["id"]
    return None


def apply_result(conn, cfg, contact: dict, res: Result, need_face: bool) -> str:
    """Write one harvest result to the DB (and the face to the KYC folder). Returns the log label."""
    h = cfg.get("harvest") or {}
    changes: dict = {"enrich_last_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    label = res.status
    if res.status == "done" and res.profile_name and name_score(res.profile_name, contact["full_name"]) < NAME_MATCH_MIN:
        res = Result("no_profile", error=f"LinkedIn profile found was '{res.profile_name}', not this person",
                     debug_text=res.debug_text)
        label = "no_profile"
    extra = db.jload(contact.get("extra"), {})
    if res.status == "done":
        empty = not (res.headline or res.about or res.experience)
        changes.update({
            "linkedin_summary": build_summary(res.headline, res.about, int(h.get("summary_chars", 500))),
            "linkedin_experience": db.jdump(res.experience),
            "linkedin_profile_url": res.profile_url,
        })
        if empty:
            # profile opened but nothing readable: retry later rather than calling it done
            changes.update({"enrich_status": "failed", "enrich_attempts": (contact.get("enrich_attempts") or 0) + 1,
                            "enrich_error": "profile opened but no headline/About/Experience could be read "
                                            "(page text saved in data/debug)"})
            label = "empty"
            save_debug(cfg, contact, res.debug_text, "profile")
        else:
            changes.update({"enrich_status": "done", "enrich_error": None})
            if not (res.headline and res.experience):
                # summary captured, but the headline/current-role or work history is still missing:
                # save what the page showed so this can be diagnosed without a separate --diagnose run
                label = "done/partial"
                save_debug(cfg, contact, res.debug_text, "partial")
        if not contact.get("linkedin_contact_url") and res.profile_url:
            changes["linkedin_contact_url"] = res.profile_url
        title = current_title(res.experience, res.headline)
        if not contact.get("role") and title:
            changes["role"] = title[:100]
            extra["role_status"] = "from LinkedIn"
        # department from the LinkedIn title (beats the spreadsheet role, never a manual choice)
        dept = classify_department(title, res.headline)
        if dept and DEPT_RANK["linkedin"] >= DEPT_RANK.get(contact.get("department_source"), 0):
            changes.update({"department": dept, "department_source": "linkedin"})
        # still at this company? LinkedIn's current role decides
        if res.location and LOC_RANK.get("linkedin", 0) >= LOC_RANK.get(contact.get("location_source"), 0):
            city, province, country = split_location(res.location)
            changes.update({"city": city, "province": province, "country": country, "location_source": "linkedin"})
        now_at = current_company(res.experience, res.headline)
        changes["linkedin_current_company"] = now_at
        if now_at and contact.get("company"):
            if same_company(now_at, contact["company"]):
                changes.update({"employment_status": "current", "moved_to_account_id": None})
            else:
                changes.update({"employment_status": "moved", "moved_to_account_id": find_account(conn, now_at)})
                label += "/moved"
        company = norm_company(contact.get("company"))
        blob = norm_company(" ".join([res.headline or ""] + [e.get("company") or "" for e in res.experience]))
        extra["match_confidence"] = "name+company" if (company and company in blob) or res.match_how == "name+company" \
            else "name-only" if res.match_how in (None, "name-only") else res.match_how
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
                label = f"{label}/no_photo"
    elif res.status == "no_profile":
        changes.update({"enrich_status": "no_profile", "enrich_error": res.error})
        if need_face:
            changes["image_status"] = "no_photo"
        save_debug(cfg, contact, (res.error or "") + "\n\n" + (res.debug_text or ""), "no_profile")
    else:
        changes.update({"enrich_status": "failed", "enrich_error": (res.error or "")[:500],
                        "enrich_attempts": (contact.get("enrich_attempts") or 0) + 1})
        save_debug(cfg, contact, (res.error or "") + "\n\n" + (res.debug_text or ""), "failed")
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


RETRY_SQL = """UPDATE contacts SET enrich_status = 'pending', enrich_attempts = 0, enrich_error = NULL
               WHERE contact_status = 'active' AND (
                     enrich_status IN ('no_profile', 'failed')
                  OR (enrich_status = 'done' AND coalesce(linkedin_summary, '') = ''
                      AND coalesce(linkedin_experience, '') IN ('', '[]')))"""


VERIFY_CHECKS = ("footer_polluted", "empty_but_marked_done", "partial", "stuck_unknown_employment",
                "duplicate_profile_url")


def verify(conn, cfg, fix: bool = True) -> dict:
    """Data-quality self-check over everyone already harvested — run this any time, no LinkedIn needed.

    Finds:
      footer_polluted        — a stored summary is actually LinkedIn's page footer (the bug fixed in
                                this build); always unambiguous, always re-queued when fix=True.
      empty_but_marked_done  — marked done under an earlier, looser definition of "empty" but really
                                has nothing usable; re-queued when fix=True.
      partial                — has a summary but no role or work history; reported, not auto-retried
                                (the summary is still useful, and a retry might not do better).
      stuck_unknown_employment — done, with a role or work history captured, but employment_status
                                was never set to current/moved; usually clears itself after a retry.
      duplicate_profile_url  — two different contacts point at the same LinkedIn profile: a likely
                                mismatch that needs a person to look at it, never auto-fixed.

    With fix=True (the default), footer_polluted and empty_but_marked_done rows have their bad
    summary/experience cleared and are set back to 'pending' so the next harvest run retries them.
    """
    report: dict = {k: [] for k in VERIFY_CHECKS}
    rows = db.rows(conn, """SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
                            WHERE c.enrich_status = 'done'""")
    seen_urls: dict[str, list] = {}
    requeue: set[int] = set()
    for r in rows:
        summary = r["linkedin_summary"] or ""
        exp = db.jload(r["linkedin_experience"], [])
        entry = {"id": r["id"], "name": r["full_name"], "company": r["company"]}
        if _FOOTER_RE.search(summary):
            report["footer_polluted"].append(entry)
            requeue.add(r["id"])
        elif not (summary.strip() and (r["role"] or exp)):
            report["empty_but_marked_done"].append(entry)
            requeue.add(r["id"])
        elif not (r["role"] and exp):
            report["partial"].append(entry)
        if r["employment_status"] == "unknown" and (r["role"] or exp):
            report["stuck_unknown_employment"].append(entry)
        if r["linkedin_profile_url"]:
            seen_urls.setdefault(r["linkedin_profile_url"], []).append(entry)
    report["duplicate_profile_url"] = [{"url": u, "contacts": es} for u, es in seen_urls.items() if len(es) > 1]
    if fix and requeue:
        with conn:
            for cid in requeue:
                db.update(conn, "contacts", cid, {"enrich_status": "pending", "enrich_attempts": 0,
                                                   "linkedin_summary": None, "linkedin_experience": None,
                                                   "enrich_error": "re-queued by --verify (bad stored data)"})
    report["requeued"] = sorted(requeue)
    return report


def print_verify_report(report: dict) -> None:
    labels = {"footer_polluted": "Summary was actually LinkedIn's page footer",
             "empty_but_marked_done": "Marked done but nothing usable was captured",
             "partial": "Summary captured, but role/work history is missing",
             "stuck_unknown_employment": "Employment status never resolved despite having data",
             "duplicate_profile_url": "Two contacts point at the same LinkedIn profile"}
    total = sum(len(report[k]) for k in VERIFY_CHECKS)
    if not total:
        print("Nothing to flag — everyone already harvested looks complete.")
        return
    for key in VERIFY_CHECKS:
        items = report[key]
        if not items:
            continue
        print(f"\n{labels[key]} ({len(items)}):")
        if key == "duplicate_profile_url":
            for d in items[:30]:
                print(f"  {d['url']}: " + ", ".join(f"#{c['id']} {c['name']}" for c in d["contacts"]))
        else:
            for e in items[:30]:
                print(f"  #{e['id']} {e['name']} ({e['company'] or '-'})")
        if len(items) > 30:
            print(f"  ... and {len(items) - 30} more")
    if report["requeued"]:
        print(f"\nRe-queued {len(report['requeued'])} contact(s) for another try — run a normal harvest to pick them up.")


def report(conn, n: int) -> None:
    for r in db.rows(conn, """SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
                              WHERE c.enrich_last_at IS NOT NULL ORDER BY c.enrich_last_at DESC LIMIT ?""", [n]):
        exp = db.jload(r["linkedin_experience"], [])
        print(f"#{r['id']} {r['full_name']} ({r['company'] or '-'})  [{r['enrich_status']}] face={r['image_status']}")
        print(f"    role: {r['role'] or '-'}   department: {r['department'] or '-'}   "
              f"employment: {r['employment_status']}" + (f" -> now at {r['linkedin_current_company']}"
                                                         if r["employment_status"] == "moved" else ""))
        print(f"    summary: {(r['linkedin_summary'] or '(none)')[:160]}")
        print(f"    work history: {len(exp)} role(s)" + "".join(
            f"\n      - {e.get('title')} @ {e.get('company') or '?'} ({e.get('dates')})" for e in exp[:3]))
        if r["enrich_error"]:
            print(f"    note: {r['enrich_error']}")
        elif r["enrich_status"] == "done" and not (r["role"] and exp):
            dbg = cfg.data_dir / "debug" / f"partial_{r['id']}.txt"
            print(f"    note: summary captured but role/work history is still missing"
                  f"{' — see ' + str(dbg) if dbg.exists() else ''}")


def diagnose(conn, cfg, who: str, driver_factory, log=print) -> Path | None:
    """Look one person up and print exactly what was read. Writes nothing to the DB."""
    c = db.one(conn, """SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
                        WHERE c.id = ? OR lower(c.full_name) LIKE ? ORDER BY c.priority IS NULL, c.priority LIMIT 1""",
               [int(who) if who.isdigit() else -1, f"%{who.lower()}%"])
    if not c:
        log(f"No contact matches '{who}'.")
        return None
    log(f"Diagnosing #{c['id']} {c['full_name']} ({c.get('company')}) …")
    driver = driver_factory()
    try:
        res = driver.fetch(c, need_face=False)
    finally:
        driver.close()
    with conn:
        conn.execute("INSERT INTO harvest_log(contact_id, day, result, detail) VALUES (?,?,?,?)",
                     [c["id"], date.today().isoformat(), "diagnose", res.status])
    log(f"  result:     {res.status}  {res.error or ''}")
    log(f"  matched by: {res.match_how}   profile: {res.profile_url}   name on page: {res.profile_name}")
    log(f"  headline:   {res.headline}")
    log(f"  about:      {(res.about or '')[:300]}")
    log(f"  current company: {current_company(res.experience, res.headline)}")
    for e in res.experience:
        log(f"  role:       {e}")
    dbg = cfg.data_dir / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    path = dbg / f"diagnose_{c['id']}.txt"
    path.write_text(res.debug_text or "(no page text)", encoding="utf-8")
    log(f"  page text saved to {path}")
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="max contacts this run")
    ap.add_argument("--test", action="store_true", help="5-contact test run")
    ap.add_argument("--cap", type=int, help="override harvest.daily_cap for today")
    ap.add_argument("--ids", help="comma-separated contact ids")
    ap.add_argument("--segment", choices=["customer", "competitor", "internal"])
    ap.add_argument("--dry-run", action="store_true", help="print the queue only")
    ap.add_argument("--retry", "--retry-empty", dest="retry", action="store_true",
                    help="re-queue everyone without a summary yet (no profile found, failed, or came back empty)")
    ap.add_argument("--report", type=int, metavar="N", help="show what was stored for the last N harvested contacts")
    ap.add_argument("--diagnose", metavar="NAME_OR_ID", help="look up one person and print what was read (no DB writes)")
    ap.add_argument("--verify", action="store_true",
                    help="self-check everyone already harvested for bad/incomplete data and re-queue what's fixable "
                        "(also runs automatically, quietly, at the start of every normal run)")
    ap.add_argument("--no-fix", action="store_true", help="with --verify, only report — don't re-queue anything")
    ap.add_argument("--headless", action="store_true", help="not recommended (LinkedIn is stricter)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    limit = 5 if args.test else args.limit
    factory = lambda: LinkedInDriver(cfg, headless=args.headless)  # noqa: E731
    if args.report:
        report(conn, args.report)
        return
    if args.diagnose:
        diagnose(conn, cfg, args.diagnose, factory)
        return
    if args.verify:
        rep = verify(conn, cfg, fix=not args.no_fix)
        print_verify_report(rep)
        return
    if args.retry:
        with conn:
            n = conn.execute(RETRY_SQL).rowcount
        print(f"Re-queued {n} contact(s) that have no LinkedIn summary yet.")
        if not (args.test or args.limit):
            return
    if args.dry_run:
        rows = queue(conn, cfg, limit or 50, ids, args.segment)
        for r in rows:
            print(f"{r['id']:>5}  p={r['priority']}  {r['segment']:<10} {r['full_name']:<30} {r.get('company') or ''}"
                  f"  face={'need' if r['image_status'] == 'none' else r['image_status']}  summary={r['enrich_status']}")
        print(f"\n{len(rows)} shown · used today: {used_today(conn)}/{(cfg.get('harvest') or {}).get('daily_cap', 40)}")
        return
    # a quiet, automatic self-check before every real run: bad old data never sits unnoticed
    silent = verify(conn, cfg, fix=True)
    fixed = len(silent["requeued"])
    if fixed:
        print(f"(self-check: re-queued {fixed} contact(s) with bad or incomplete stored data)")
    stats = run(conn, cfg, factory, limit, args.cap, ids, args.segment)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
