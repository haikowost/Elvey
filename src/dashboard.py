"""Local dashboard: People Tree + Zoho review-and-select.

    python -m src.dashboard            # http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import analyze, chat, db, export, harvest, orgchart, zoho
from .config import load_config
from .images import coverage, update_account_kyc_status
from .util import ACCOUNT_ROLES, CONTACT_CLASSES, DEPARTMENTS, norm_company

CONTACT_STATUSES = ("active", "left", "not_relevant")

STATIC = Path(__file__).with_name("static")
BRANCH_ORDER = ("competitor", "internal", "customer", "supplier")


class Selection(BaseModel):
    entity: str
    local_id: int
    fields: list[str] | None = None
    photo: bool | None = None


class ContactUpdate(BaseModel):
    department: str | None = None          # one of util.DEPARTMENTS, or "" to clear
    contact_status: str | None = None      # active | left | not_relevant
    status_note: str | None = None
    move_to_account_id: int | None = None  # re-allocate to an existing account
    create_account: str | None = None      # re-allocate to a new account with this name
    keep_account: bool | None = None       # LinkedIn "moved" flag is wrong: keep them where they are
    linkedin_url: str | None = None        # pin the exact profile when search can't find them; re-queues
    contact_class: str | None = None       # engaged | lead | backlog, or "" to clear (v4 §2)
    reports_to_id: int | None = None       # contact-level org chart; 0 clears it (v4 §3)
    segment_override: str | None = None    # pins one account_role; "" clears it, back to inheriting (v4 §1)


class ChatMessage(BaseModel):
    contact_id: int
    message: str


class PushRequest(BaseModel):
    selections: list[Selection]
    live: bool = False


class QuoteExportRequest(BaseModel):
    contact_ids: list[int]


def _effective_roles(c: dict, account_roles: dict[int, list[str]]) -> list[str]:
    """Which People Tree branch(es) a contact shows under. Internal contacts are always just
    internal; a `segment_override` pins one role; otherwise a contact inherits every role its
    account carries (v4 §1) — a dual customer+supplier account's contacts show under both tabs
    until segment_override starts being set to split them."""
    if c["segment"] == "internal":
        return ["internal"]
    if c["segment_override"]:
        return [c["segment_override"]]
    return account_roles.get(c["account_id"]) or [c["segment"]]


def people_tree(conn, cfg) -> dict:
    accounts = {a["id"]: a for a in db.rows(conn, "SELECT * FROM accounts")}
    account_roles: dict[int, list[str]] = {}
    for r in db.rows(conn, "SELECT account_id, role FROM account_roles ORDER BY role"):
        account_roles.setdefault(r["account_id"], []).append(r["role"])
    contacts = db.rows(conn, "SELECT * FROM contacts ORDER BY priority IS NULL, priority, full_name")
    names_by_id = {c["id"]: c["full_name"] for c in contacts}
    groups: dict[tuple, dict] = {}
    for c in contacts:
        a = accounts.get(c["account_id"]) or {}
        extra_c = db.jload(c["extra"], {})
        person = {
            "id": c["id"], "name": c["full_name"], "role": c["role"], "email": c["email"], "cell": c["cell"] or c["tel"],
            "tel": c["tel"], "mobile": c["cell"], "company": a.get("name"), "segment": c["segment"],
            "role_status": extra_c.get("role_status"), "enrich_error": c["enrich_error"],
            "enriched_at": c["enrich_last_at"],
            "account_id": c["account_id"], "department": c["department"], "department_source": c["department_source"],
            "employment_status": c["employment_status"], "now_at": c["linkedin_current_company"],
            "moved_to": ({"id": c["moved_to_account_id"], "name": (accounts.get(c["moved_to_account_id"]) or {}).get("name")}
                         if c["moved_to_account_id"] else None),
            "contact_status": c["contact_status"], "status_note": c["status_note"],
            "previous_accounts": extra_c.get("previous_accounts") or [],
            "city": c["city"], "province": c["province"], "country": c["country"],
            "face": f"/faces/{c['image_filename']}" if c["image_filename"] and c["image_status"] in ("downloaded", "manual") else None,
            "image_status": c["image_status"], "enrich_status": c["enrich_status"],
            "summary": c["linkedin_summary"], "experience": db.jload(c["linkedin_experience"], []),
            "linkedin": c["linkedin_profile_url"] or c["linkedin_contact_url"], "priority": c["priority"],
            "match_confidence": extra_c.get("match_confidence"), "category": c["category"] or extra_c.get("category"),
            "allocated_rep": c["allocated_rep"], "allocated": bool(c["allocated"]),
            "in_zoho": c["in_zoho"], "in_makdb": c["in_makdb"], "role_source": c["role_source"],
            "contact_class": c["contact_class"], "segment_override": c["segment_override"],
            "reports_to": ({"id": c["reports_to_id"], "name": names_by_id.get(c["reports_to_id"])}
                            if c["reports_to_id"] else None),
            "zoho": bool(c["zoho_contact_id"]),
        }
        for role in _effective_roles(c, account_roles):
            if role not in BRANCH_ORDER:
                continue
            key_name = c["org_group"] if role == "internal" and c["org_group"] else (a.get("name") or "(no account)")
            key = (role, key_name)
            if key not in groups:
                # business-case fields and sheet-hint tags mean something for customer/supplier accounts
                is_biz = role in ("customer", "supplier")
                extra = db.jload(a.get("extra"), {}) if is_biz else {}
                tags = [t for t in (
                    f"Cat {extra['category']}" if extra.get("category") else None,
                    extra.get("axis_partner"),
                    f"Tasha: {extra['tasha_visit']}" if extra.get("tasha_visit") else None,
                    "Allocation conflict" if (extra.get("allocation_conflict") or "").upper() == "Y" else None,
                    f"KYC {a['kyc_status']}" if a.get("kyc_status") and role != "internal" else None,
                ) if t]
                groups[key] = {
                    "segment": role, "title": key_name, "account_id": a.get("id") if key_name == a.get("name") else None,
                    "rank": a.get("rank") if key_name == a.get("name") else None,
                    **({"division": a.get("division"), "rep": a.get("rep"), "sellout": a.get("latest_sellout"),
                        "brand_focus": a.get("brand_focus"), "allocation": a.get("allocation"),
                        "branch": a.get("branch")} if is_biz else
                       {"division": None, "rep": None, "sellout": None, "brand_focus": None, "allocation": None,
                        "branch": None}),
                    "concerns": (extra.get("tasha_concerns") or "").strip(" ;") or None,
                    "tags": tags, "zoho": bool(a.get("zoho_account_id")), "people": [],
                    "account_roles": account_roles.get(a.get("id"), []),
                }
            groups[key]["people"].append(person)
    out = {seg: [] for seg in BRANCH_ORDER}
    for g in groups.values():
        # the Customers branch surfaces the "actually being worked" contacts first (v4 §2)
        g["people"].sort(key=lambda p: (p["contact_class"] != "engaged", not p["allocated"],
                                         p["priority"] is None, p["priority"] or 0, p["name"]))
        out[g["segment"]].append(g)
    for seg in out:
        out[seg].sort(key=lambda g: (g["rank"] is None, g["rank"] or 0, -(g["sellout"] or 0), g["title"]))
    return out


def edit_contact(conn, contact_id: int, req: dict) -> dict:
    """Apply a manual edit from the contact card. Manual department choices are never overwritten."""
    c = db.one(conn, "SELECT * FROM contacts WHERE id = ?", [contact_id])
    if not c:
        raise ValueError("contact not found")
    changes: dict = {}
    extra = db.jload(c["extra"], {})
    if "department" in req:
        dept = req["department"] or None
        if dept and dept not in DEPARTMENTS:
            raise ValueError(f"unknown department '{dept}'")
        changes.update({"department": dept, "department_source": "manual" if dept else None})
    if "contact_status" in req:
        if req["contact_status"] not in CONTACT_STATUSES:
            raise ValueError(f"unknown status '{req['contact_status']}'")
        changes["contact_status"] = req["contact_status"]
    if "status_note" in req:
        changes["status_note"] = req["status_note"] or None
    if "contact_class" in req:
        cls = req["contact_class"] or None
        if cls and cls not in CONTACT_CLASSES:
            raise ValueError(f"unknown contact class '{cls}'")
        changes["contact_class"] = cls
    if "reports_to_id" in req:
        manager_id = req["reports_to_id"] or None
        if manager_id:
            if manager_id == contact_id:
                raise ValueError("a contact cannot report to themselves")
            if not db.one(conn, "SELECT id FROM contacts WHERE id = ?", [manager_id]):
                raise ValueError("manager not found")
        changes["reports_to_id"] = manager_id
    if "segment_override" in req:
        override = req["segment_override"] or None
        if override and override not in ACCOUNT_ROLES:
            raise ValueError(f"unknown role '{override}'")
        changes["segment_override"] = override
    target = None
    if req.get("create_account"):
        name = req["create_account"].strip()
        existing = db.one(conn, "SELECT id FROM accounts WHERE name_norm = ?", [norm_company(name)])
        target = existing["id"] if existing else db.insert(conn, "accounts", {
            "name": name, "name_norm": norm_company(name), "segment": "customer", "source": "linkedin"})
    elif req.get("move_to_account_id"):
        target = req["move_to_account_id"]
    if target and target != c["account_id"]:
        acct = db.one(conn, "SELECT id, name, segment FROM accounts WHERE id = ?", [target])
        if not acct:
            raise ValueError("account not found")
        old = db.one(conn, "SELECT id, name FROM accounts WHERE id = ?", [c["account_id"]]) if c["account_id"] else None
        if old:
            extra.setdefault("previous_accounts", []).append(
                {"id": old["id"], "name": old["name"], "until": time.strftime("%Y-%m-%d")})
        changes.update({"account_id": acct["id"], "segment": acct["segment"], "employment_status": "current",
                        "moved_to_account_id": None, "contact_status": "active"})
    if req.get("keep_account"):
        extra["move_dismissed"] = c["linkedin_current_company"]
        changes.update({"employment_status": "current", "moved_to_account_id": None})
    changes["extra"] = db.jdump(extra)
    with conn:
        db.update(conn, "contacts", contact_id, changes)
        update_account_kyc_status(conn)
    if req.get("linkedin_url"):
        harvest.set_manual_url(conn, contact_id, req["linkedin_url"])
    return db.one(conn, "SELECT * FROM contacts WHERE id = ?", [contact_id])


def create_app(cfg=None) -> FastAPI:
    cfg = cfg or load_config()
    conn = db.connect(cfg.db_path)
    lock = threading.Lock()  # one sqlite connection shared by the worker threads
    app = FastAPI(title="Elvey KYC")

    def client():
        try:
            return zoho.ZohoClient.from_env(cfg)
        except zoho.ZohoError as e:
            raise HTTPException(400, str(e))

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/faces/{filename}")
    def face(filename: str):
        folder = cfg.kyc_folder.resolve()
        path = (folder / filename).resolve()
        if path.parent != folder or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/people")
    def people():
        with lock:
            return people_tree(conn, cfg)

    @app.get("/api/stats")
    def stats():
        with lock:
            q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            reps = [r[0] for r in conn.execute(
                "SELECT DISTINCT allocated_rep FROM contacts WHERE allocated_rep IS NOT NULL ORDER BY 1")]
            cats = [r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM contacts WHERE category IS NOT NULL ORDER BY 1")]
            class_counts = {r[0]: r[1] for r in conn.execute(
                "SELECT COALESCE(contact_class, 'unclassified'), count(*) FROM contacts GROUP BY 1")}
            role_counts = {r[0]: r[1] for r in conn.execute(
                "SELECT role, count(*) FROM account_roles GROUP BY 1")}
            return {"accounts": q("SELECT count(*) FROM accounts"), "contacts": q("SELECT count(*) FROM contacts"),
                    "coverage": coverage(conn), "zoho_pulled_at": zoho.get_meta(conn, "zoho_pulled_at"),
                    "live_enabled": bool((cfg.get("zoho") or {}).get("live_enabled")),
                    "departments": list(DEPARTMENTS),
                    "relevant_departments": list((cfg.get("dashboard") or {}).get("relevant_departments") or []),
                    "allocated": q("SELECT count(*) FROM contacts WHERE allocated = 1"),
                    "unallocated": q("SELECT count(*) FROM contacts WHERE allocated = 0"),
                    "reps": reps, "categories": cats, "contact_classes": class_counts, "account_roles": role_counts,
                    "last_ingest": (conn.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone() or [None])[0]}

    @app.get("/api/analyze")
    def analyze_panels():
        with lock:
            return analyze.panels(conn)

    @app.post("/api/export/quote")
    def export_quote(req: QuoteExportRequest):
        with lock:
            rows = export.quote_rows(conn, req.contact_ids)
        if not rows:
            raise HTTPException(400, "No matching contacts to export")
        content = export.rows_to_xlsx(rows)
        filename = f"elvey-quote-export-{time.strftime('%Y%m%d')}.xlsx"
        return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/accounts")
    def accounts_list():
        with lock:
            accts = db.rows(conn, "SELECT id, name, segment, rank FROM accounts ORDER BY rank IS NULL, rank, name")
            for a in accts:
                a["roles"] = orgchart.roles_for_account(conn, a["id"])
            return accts

    @app.post("/api/contacts/{contact_id}")
    def update_contact(contact_id: int, req: ContactUpdate):
        with lock:
            try:
                return {"ok": True, "contact": edit_contact(conn, contact_id, req.model_dump(exclude_none=True))}
            except ValueError as e:
                raise HTTPException(400, str(e))

    @app.get("/api/zoho/diff")
    def diff():
        with lock:
            d = zoho.compute_diff(conn, cfg)
        d["setup_steps"] = zoho.setup_steps(d["missing_fields"], cfg) if any(d["missing_fields"].values()) else None
        d["live_enabled"] = bool((cfg.get("zoho") or {}).get("live_enabled"))
        return d

    @app.get("/api/verify")
    def verify_report():
        with lock:
            return harvest.verify(conn, cfg, fix=False)

    @app.get("/api/chat/queue")
    def chat_queue():
        with lock:
            return chat.build_queue(conn, cfg)

    @app.post("/api/chat")
    def chat_message(req: ChatMessage):
        with lock:
            contact = db.one(conn, "SELECT c.*, a.name AS company FROM contacts c "
                                   "LEFT JOIN accounts a ON a.id = c.account_id WHERE c.id = ?", [req.contact_id])
            if not contact:
                raise HTTPException(404, "contact not found")
            accounts = db.rows(conn, "SELECT id, name FROM accounts")
            result = chat.parse_correction(req.message, contact, accounts)
            applied = None
            if result.action == "apply":
                try:
                    applied = edit_contact(conn, req.contact_id, {k: v for k, v in result.payload.items() if v is not None})
                except ValueError as e:
                    return {"action": "unclear", "reply": f"Couldn't apply that: {e}", "contact": None}
            return {"action": result.action, "reply": result.reply, "contact": applied}

    @app.post("/api/verify")
    def verify_and_fix():
        with lock:
            return harvest.verify(conn, cfg, fix=True)

    @app.post("/api/zoho/pull")
    def pull():
        c = client()
        with lock:
            try:
                return {"pulled": zoho.pull(conn, cfg, c, log=lambda *_: None)}
            except zoho.ZohoError as e:
                raise HTTPException(502, str(e))

    @app.post("/api/zoho/push")
    def push(req: PushRequest):
        sels = [s.model_dump(exclude_none=True) for s in req.selections]
        with lock:
            try:
                return zoho.push(conn, cfg, client() if req.live else None, sels, dry_run=not req.live)
            except PermissionError as e:
                return JSONResponse({"error": str(e)}, status_code=403)
            except zoho.ZohoError as e:
                raise HTTPException(502, str(e))

    @app.get("/api/zoho/log")
    def sync_log(limit: int = 200):
        with lock:
            return db.rows(conn, "SELECT * FROM zoho_sync_log ORDER BY id DESC LIMIT ?", [limit])

    return app


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    d = cfg.get("dashboard") or {}
    import uvicorn

    port = args.port or int(d.get("port", 8765))
    print(f"Elvey KYC dashboard → http://{d.get('host', '127.0.0.1')}:{port}")
    uvicorn.run(create_app(cfg), host=d.get("host", "127.0.0.1"), port=port, log_level="warning")


if __name__ == "__main__":
    main()
