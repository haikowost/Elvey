"""The correction chat: a plain-English way to fix the handful of contacts each harvest run
can't resolve on its own (no matching LinkedIn profile, or a match that's only a name guess).

Not a live AI chat — no external call, no API key, nothing new to configure. It's a small parser
over the same fields the dashboard contact card and `--set-url` already write, applied to
whichever person the batch is currently showing you. That keeps it honest: it can only do what
was already tested and working, phrased as one line instead of several clicks.

    "https://www.linkedin.com/in/marie-deysel-1503a53a/"   -> pins their exact profile, re-queues
    "left" / "no longer there" / "resigned"                -> marks no longer at the company
    "not relevant" / "skip them"                            -> marks not relevant
    "active" / "keep" / "that's right"                       -> confirms active (undoes a flagged move)
    "finance" / "department: sales"                          -> sets the department
    "now at Beta Integrators" / "moved to Beta Integrators"  -> re-allocates to that account
    "skip" / "next"                                           -> leaves them as-is, moves on
"""
from __future__ import annotations

import re

from . import db, harvest
from .util import DEPARTMENTS, linkedin_url, norm_company

SKIP_WORDS = {"skip", "next", "pass", "later", "leave it", "leave"}
STOP_WORDS = {"stop", "done", "quit", "that's all", "thats all", "exit"}
_LEFT_RE = re.compile(r"\b(left|no longer|resigned|not there|not with|moved on|gone)\b", re.IGNORECASE)
_NOT_RELEVANT_RE = re.compile(r"\b(not relevant|irrelevant|not applicable|skip (them|him|her)|n/?a\b)\b", re.IGNORECASE)
_ACTIVE_RE = re.compile(r"\b(active|keep( them| him| her)?|correct|that'?s right|still (there|active|correct)|"
                        r"linkedin('s)? wrong)\b", re.IGNORECASE)
_MOVE_RE = re.compile(r"(?:\b(?:now at|moved to|works? (?:at|for)|joined)\b|->|→)\s*(.+)$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S*linkedin\.com/in/\S+", re.IGNORECASE)
_DEPT_LABEL_RE = re.compile(r"\bdepartment\s*[:\-]?\s*(.+)$", re.IGNORECASE)
_DEPT_WORDS = {d.lower(): d for d in DEPARTMENTS}


class Correction:
    """What a chat line means. `payload` is what to POST to /api/contacts/{id}; `reply` is shown
    back in the chat. `payload` is None for skip/stop/unrecognised — nothing gets applied."""

    def __init__(self, action: str, payload: dict | None, reply: str):
        self.action, self.payload, self.reply = action, payload, reply  # action: apply | skip | stop | unclear


def _find_account(text: str, accounts: list[dict]) -> dict | None:
    key = norm_company(text)
    exact = [a for a in accounts if norm_company(a["name"]) == key]
    if exact:
        return exact[0]
    contains = [a for a in accounts if key in norm_company(a["name"]) or norm_company(a["name"]) in key]
    return contains[0] if len(contains) == 1 else None


def parse_correction(text: str, contact: dict, accounts: list[dict] | None = None) -> Correction:
    """`contact` is the person currently on screen — a row from /api/chat/queue or /api/people.
    `accounts` (id, name[, name_norm]) is used to resolve 'now at <company>' to an existing account."""
    t = text.strip()
    low = t.lower().strip(" .!")
    name = contact.get("name") or contact.get("full_name") or "they"
    accounts = accounts or []

    if not t:
        return Correction("unclear", None, "Type a correction, or 'skip' to move on.")
    if low in SKIP_WORDS:
        return Correction("skip", None, f"Skipped {name}.")
    if low in STOP_WORDS:
        return Correction("stop", None, "Stopping here — come back to the rest any time.")

    m_url = _URL_RE.search(t)
    url = linkedin_url(m_url.group(0)) if m_url else None
    if url:
        return Correction("apply", {"linkedin_url": url},
                          f"Pinned {name}'s LinkedIn profile to {url}. Re-queued for the next harvest run.")

    if _LEFT_RE.search(low):
        note_words = _LEFT_RE.sub("", t).strip(" ,.-") or None
        return Correction("apply", {"contact_status": "left", "status_note": note_words},
                          f"Marked {name} as no longer at their company.")

    if _NOT_RELEVANT_RE.search(low):
        return Correction("apply", {"contact_status": "not_relevant"}, f"Marked {name} as not relevant.")

    m = _DEPT_LABEL_RE.search(t)
    dept_text = (m.group(1) if m else t).strip().lower()
    if dept_text in _DEPT_WORDS:
        dept = _DEPT_WORDS[dept_text]
        return Correction("apply", {"department": dept}, f"Set {name}'s department to {dept}.")

    m = _MOVE_RE.search(t)
    if m:
        company = m.group(1).strip(" .!\"'")
        if not company:
            return Correction("unclear", None, "Moved to where? Give me the company name.")
        acct = _find_account(company, accounts)
        if acct:
            return Correction("apply", {"move_to_account_id": acct["id"]}, f"Moved {name} to {acct['name']}.")
        return Correction("apply", {"create_account": company},
                          f"'{company}' isn't in the database yet — added it and moved {name} there.")

    if _ACTIVE_RE.search(low):
        if contact.get("employment_status") == "moved":
            return Correction("apply", {"keep_account": True},
                              f"Kept {name} at {contact.get('company') or 'their current account'} — "
                              "noted that LinkedIn's suggestion was wrong.")
        return Correction("apply", {"contact_status": "active"}, f"Confirmed {name} as active.")

    return Correction("unclear", None,
                      "Not sure what to do with that. Try a LinkedIn profile URL, 'left', 'not relevant', "
                      "a department name, 'now at <company>', or 'skip'.")


def build_queue(conn, cfg) -> list[dict]:
    """Everyone the automated run couldn't resolve on its own, worst-blocked first: no profile
    found at all, then a name-only match, then a duplicate LinkedIn match. Uses harvest.verify
    (report-only — never writes) for the last two, so the two queues can never disagree."""
    out: list[dict] = []
    seen: set[int] = set()

    def add(row: dict, reason: str, extra: dict | None = None) -> None:
        if row["id"] in seen:
            return
        seen.add(row["id"])
        out.append({"id": row["id"], "name": row["full_name"], "company": row.get("company"),
                    "segment": row["segment"], "priority": row["priority"], "reason": reason,
                    "linkedin": row.get("linkedin_contact_url") or row.get("linkedin_profile_url"),
                    "employment_status": row.get("employment_status"),
                    "now_at": row.get("linkedin_current_company"), **(extra or {})})

    no_profile = db.rows(conn, """SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
                                  WHERE c.contact_status = 'active' AND c.enrich_status = 'no_profile'
                                  ORDER BY c.priority IS NULL, c.priority""")
    for r in no_profile:
        add(r, "LinkedIn search couldn't find a matching profile")

    rep = harvest.verify(conn, cfg, fix=False)
    by_id = {r["id"]: r for r in db.rows(conn, """SELECT c.*, a.name AS company FROM contacts c
                                                  LEFT JOIN accounts a ON a.id = c.account_id""")}
    for e in rep["unsure_match"]:
        r = by_id.get(e["id"])
        if r and r["contact_status"] == "active":
            add(r, f"matched by name only — not confirmed against {r.get('company') or 'their company'}")
    for dup in rep["duplicate_profile_url"]:
        names = ", ".join(c["name"] for c in dup["contacts"])
        for c in dup["contacts"]:
            r = by_id.get(c["id"])
            if r and r["contact_status"] == "active":
                add(r, f"same LinkedIn profile as {names.replace(c['name'] + ', ', '').replace(', ' + c['name'], '')}")
    for e in rep["stuck_unknown_employment"]:
        r = by_id.get(e["id"])
        if r and r["contact_status"] == "active" and r["employment_status"] == "unknown":
            add(r, "has a role/work history but nothing says whether it's still current — a quick glance would help")
    return out
