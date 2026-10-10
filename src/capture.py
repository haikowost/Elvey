"""'Send to KYC' bookmarklet: store a LinkedIn profile Haiko is already looking at - no automation,
no Claude, no tokens.

The bookmarklet (src/static/bookmarklet.js, installed from http://127.0.0.1:<port>/bookmarklet) runs
the harvester's own page reader (harvest.JS_PROFILE) on the profile open in his normal Chrome and
POSTs the result to /api/capture. Here it goes through the same parsing (parse_profile /
location_from) and the same writer (harvest.apply_result) as an automated harvest, so a captured
profile is indistinguishable from a harvested one - except it never counts against the daily cap.

Matching: the profile URL first (a contact already pinned to that /in/ slug), then name + current
company. Anything not matched exactly once waits in the `captures` table for the "match or create"
picker at /capture (linked from the dashboard).
"""
from __future__ import annotations

import json
import re
import time

from . import db
from .harvest import (NAME_MATCH_MIN, Result, apply_result, current_company, downscale_jpeg, find_account,
                      is_profile_url, location_from, name_score, parse_profile)
from .util import linkedin_url, norm_company, norm_name, same_company, split_name

LINKEDIN_ORIGIN_RE = re.compile(r"^https://([a-z0-9-]+\.)*linkedin\.com$", re.IGNORECASE)
PHOTO_HOST_RE = re.compile(r"^https://media(-exp\d+)?\.licdn\.com/", re.IGNORECASE)
MAX_LINES = 600


def slug(url: str | None) -> str | None:
    """'https://za.linkedin.com/in/Bob-Jones-1a2b/?trk=x' -> 'bob-jones-1a2b'."""
    m = re.search(r"linkedin\.com/in/([^/?#\s]+)", str(url or ""), re.IGNORECASE)
    return m.group(1).lower().rstrip("/") if m else None


def _lines(v, n=MAX_LINES) -> list[str]:
    return [str(x)[:500] for x in (v or [])[:n] if isinstance(x, (str, int, float))]


def sanitize(payload: dict) -> dict:
    """Keep only the fields the parser uses, bounded in size (it's untrusted page text)."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    secs = []
    for sec in (data.get("sections") or [])[:40]:
        if isinstance(sec, dict):
            secs.append({"heading": str(sec.get("heading") or "")[:120], "lines": _lines(sec.get("lines"), 300)})
    exp = [_lines(e, 40) for e in (data.get("experience") or [])[:30] if isinstance(e, list)]
    return {
        "url": str(payload.get("url") or data.get("url") or "")[:400],
        "photo_src": str(payload.get("photo_src") or "")[:2000],
        "data": {
            "name": str(data.get("name") or "")[:200], "headline": str(data.get("headline") or "")[:400],
            "about": str(data.get("about") or "")[:4000], "experience": exp, "sections": secs,
            "topLines": _lines(data.get("topLines"), 20), "text": _lines(data.get("text")),
            "title": str(data.get("title") or "")[:300],
            "connection_degree": str(data.get("connection_degree") or "")[:10],
            "current_company_top": str(data.get("current_company_top") or "")[:200],
            "education_top": str(data.get("education_top") or "")[:200],
        },
    }


def to_result(p: dict, max_experience: int = 5, photo: bytes | None = None) -> Result:
    """A sanitized capture -> the harvester's Result (same parsing as an automated visit)."""
    data = p["data"]
    headline, about, experience = parse_profile(data, max_experience)
    name = data.get("name") or None
    if not name:
        from .harvest import name_from_title
        name = name_from_title(data.get("title"))
    url = linkedin_url(p.get("url")) if is_profile_url(p.get("url")) else None
    status = "done" if name and url else "failed"
    return Result(status, profile_url=(url or "").split("?")[0] or None, profile_name=name, headline=headline,
                  about=about, experience=experience, location=location_from(data), photo=photo,
                  match_how="capture", connection_degree=data.get("connection_degree") or None,
                  current_company_top=data.get("current_company_top") or None,
                  education_top=data.get("education_top") or None,
                  error=None if status == "done" else "capture had no name or no linkedin.com/in/ URL")


def summary(res: Result) -> dict:
    """What the picker shows about a capture."""
    return {"name": res.profile_name, "headline": res.headline, "url": res.profile_url, "location": res.location,
            "company": res.current_company_top or current_company(res.experience, res.headline),
            "experience": res.experience[:3]}


def _contact(conn, cid: int) -> dict | None:
    return db.one(conn, "SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id "
                        "WHERE c.id = ?", [cid])


def match(conn, res: Result) -> tuple[int | None, str, list[dict]]:
    """(contact id or None, how, candidates). Profile URL first, then name + current company."""
    s = slug(res.profile_url)
    if s:
        for r in db.rows(conn, "SELECT id, linkedin_profile_url, linkedin_contact_url FROM contacts "
                               "WHERE lower(coalesce(linkedin_profile_url, '') || ' ' || coalesce(linkedin_contact_url, '')) LIKE ?",
                         [f"%/in/{s}%"]):
            if s in (slug(r["linkedin_profile_url"]), slug(r["linkedin_contact_url"])):
                return r["id"], "profile url", []
    name = res.profile_name or ""
    company = res.current_company_top or current_company(res.experience, res.headline)
    first = norm_name(name).split()[:1]
    pool = db.rows(conn, """SELECT c.id, c.full_name, c.role, c.segment, c.contact_status, c.image_filename,
                                   c.image_status, c.kyc_tier, a.name AS company
                            FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
                            WHERE c.name_norm LIKE ?""", [f"%{first[0]}%" if first else "%"])
    scored = sorted(((name_score(name, r["full_name"]), r) for r in pool), key=lambda x: -x[0])
    named = [r for sc, r in scored if sc >= NAME_MATCH_MIN]
    at_company = [r for r in named if company and r["company"] and same_company(company, r["company"])]
    if len(at_company) == 1:
        return at_company[0]["id"], "name+company", []
    cands = [{**r, "score": round(sc)} for sc, r in scored[:8] if sc >= 60]
    return None, ("several people with this name" if len(at_company) > 1 or len(named) > 1
                  else "no contact with this name at " + (company or "their company")), cands


def fetch_photo(src: str | None, cfg) -> bytes | None:
    """LinkedIn's public CDN photo the page showed (media.licdn.com only), downscaled like the harvester's."""
    if not src or not PHOTO_HOST_RE.match(src):
        return None
    if src.startswith("data:image/"):
        return None
    h = cfg.get("harvest") or {}
    try:
        import requests

        r = requests.get(src, timeout=15)
        if r.ok and r.headers.get("content-type", "").startswith("image/"):
            return downscale_jpeg(r.content, int(h.get("image_px", 240)), float(h.get("jpeg_quality", 0.85)))
    except Exception:  # noqa: BLE001 - a missing photo must never lose the rest of the capture
        return None
    return None


def store(conn, payload: dict) -> int:
    p = sanitize(payload)
    with conn:
        return db.insert(conn, "captures", {"url": p["url"], "payload": json.dumps(p, ensure_ascii=False),
                                            "status": "pending", "received_at": time.strftime("%Y-%m-%d %H:%M:%S")})


def get(conn, capture_id: int) -> dict | None:
    row = db.one(conn, "SELECT * FROM captures WHERE id = ?", [capture_id])
    if row:
        row["payload"] = db.jload(row["payload"], {})
    return row


def apply(conn, cfg, capture_id: int, contact_id: int, how: str, photo_fetcher=None) -> dict:
    """Write capture -> contact through harvest.apply_result, mark the capture applied."""
    cap = get(conn, capture_id)
    contact = _contact(conn, contact_id)
    if not cap or not contact:
        raise ValueError("capture or contact not found")
    p = cap["payload"]
    need_face = contact["image_status"] not in ("downloaded", "manual")
    photo = (photo_fetcher or fetch_photo)(p.get("photo_src"), cfg) if need_face else None
    res = to_result(p, int((cfg.get("harvest") or {}).get("max_experience", 5)), photo)
    if res.status != "done":
        raise ValueError(res.error or "nothing usable in this capture")
    label = apply_result(conn, cfg, contact, res, need_face=bool(photo), source="capture", trusted=True)
    if res.profile_url:  # the page he was on IS their profile: pin it so search never second-guesses it
        with conn:
            db.update(conn, "contacts", contact_id, {"linkedin_contact_url": res.profile_url})
    with conn:
        db.update(conn, "captures", capture_id, {"status": "applied", "contact_id": contact_id, "match_how": how},
                  touch=False)
    return {"status": "saved", "label": label, "how": how, "contact": _brief(conn, contact_id)}


def _brief(conn, cid: int) -> dict:
    c = _contact(conn, cid) or {}
    return {"id": c.get("id"), "name": c.get("full_name"), "company": c.get("company"), "role": c.get("role"),
            "photo": c.get("image_status") in ("downloaded", "manual"), "linkedin": c.get("linkedin_profile_url")}


def receive(conn, cfg, payload: dict, photo_fetcher=None) -> dict:
    """POST /api/capture: store, try to match, apply when the match is unambiguous."""
    cid = store(conn, payload)
    cap = get(conn, cid)
    res = to_result(cap["payload"])
    if res.status != "done":
        with conn:
            db.update(conn, "captures", cid, {"status": "failed"}, touch=False)
        return {"status": "failed", "capture_id": cid, "error": res.error}
    contact_id, how, cands = match(conn, res)
    if contact_id:
        return {"capture_id": cid, **apply(conn, cfg, cid, contact_id, how, photo_fetcher)}
    return {"status": "needs_match", "capture_id": cid, "reason": how, "capture": summary(res),
            "candidates": [_cand(c) for c in cands]}


def _cand(c: dict) -> dict:
    return {"id": c["id"], "name": c["full_name"], "company": c.get("company"), "role": c.get("role"),
            "tier": c.get("kyc_tier"), "score": c.get("score"),
            "face": f"/faces/{c['image_filename']}" if c.get("image_filename") and
            c.get("image_status") in ("downloaded", "manual") else None}


def pending(conn) -> list[dict]:
    """Captures waiting for the "match or create" picker, newest first, each with its candidates."""
    out = []
    for row in db.rows(conn, "SELECT id FROM captures WHERE status = 'pending' ORDER BY id DESC LIMIT 50"):
        cap = get(conn, row["id"])
        res = to_result(cap["payload"])
        _, how, cands = match(conn, res)
        out.append({"capture_id": cap["id"], "received_at": cap["received_at"], "reason": how,
                    "capture": summary(res), "candidates": [_cand(c) for c in cands]})
    return out


def create_and_apply(conn, cfg, capture_id: int, company: str | None = None, photo_fetcher=None) -> dict:
    """Picker's "create new contact": a contact at the company the profile shows (account created if new)."""
    cap = get(conn, capture_id)
    if not cap:
        raise ValueError("capture not found")
    res = to_result(cap["payload"])
    if not res.profile_name:
        raise ValueError("this capture has no name")
    company = (company or res.current_company_top or current_company(res.experience, res.headline) or "").strip()
    with conn:
        aid = find_account(conn, company) if company else None
        if not aid and company:
            aid = db.insert(conn, "accounts", {"name": company, "name_norm": norm_company(company),
                                               "segment": "customer", "source": "linkedin"})
        seg = (db.one(conn, "SELECT segment FROM accounts WHERE id = ?", [aid]) or {}).get("segment") if aid else None
        first, last = split_name(res.profile_name)
        new_id = db.insert(conn, "contacts", {
            "full_name": res.profile_name, "name_norm": norm_name(res.profile_name), "first_name": first,
            "last_name": last, "account_id": aid, "segment": seg or "customer", "role_source": "pending",
            "extra": db.jdump({"source": "linkedin capture"})})
    return apply(conn, cfg, capture_id, new_id, "created", photo_fetcher)


def dismiss(conn, capture_id: int) -> None:
    with conn:
        db.update(conn, "captures", capture_id, {"status": "dismissed"}, touch=False)
