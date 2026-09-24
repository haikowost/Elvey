"""Local dashboard: People Tree + Zoho review-and-select.

    python -m src.dashboard            # http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import db, zoho
from .config import load_config
from .images import coverage, update_account_kyc_status
from .util import DEPARTMENTS, norm_company

CONTACT_STATUSES = ("active", "left", "not_relevant")

STATIC = Path(__file__).with_name("static")
BRANCH_ORDER = ("competitor", "internal", "customer")


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


class PushRequest(BaseModel):
    selections: list[Selection]
    live: bool = False


def people_tree(conn, cfg) -> dict:
    accounts = {a["id"]: a for a in db.rows(conn, "SELECT * FROM accounts")}
    contacts = db.rows(conn, "SELECT * FROM contacts ORDER BY priority IS NULL, priority, full_name")
    groups: dict[tuple, dict] = {}
    for c in contacts:
        a = accounts.get(c["account_id"]) or {}
        seg = c["segment"]
        key_name = c["org_group"] if seg == "internal" and c["org_group"] else (a.get("name") or "(no account)")
        key = (seg, key_name)
        if key not in groups:
            extra = db.jload(a.get("extra"), {}) if seg == "customer" else {}
            tags = [t for t in (
                f"Cat {extra['category']}" if extra.get("category") else None,
                extra.get("axis_partner"),
                f"Tasha: {extra['tasha_visit']}" if extra.get("tasha_visit") else None,
                "Allocation conflict" if (extra.get("allocation_conflict") or "").upper() == "Y" else None,
                f"KYC {a['kyc_status']}" if a.get("kyc_status") and seg != "internal" else None,
            ) if t]
            groups[key] = {
                "segment": seg, "title": key_name, "account_id": a.get("id") if key_name == a.get("name") else None,
                "rank": a.get("rank") if key_name == a.get("name") else None,
                # business-case fields only mean something for customer accounts
                **({"division": a.get("division"), "rep": a.get("rep"), "sellout": a.get("latest_sellout"),
                    "brand_focus": a.get("brand_focus"), "allocation": a.get("allocation")} if seg == "customer" else
                   {"division": None, "rep": None, "sellout": None, "brand_focus": None, "allocation": None}),
                "concerns": (extra.get("tasha_concerns") or "").strip(" ;") or None,
                "tags": tags, "zoho": bool(a.get("zoho_account_id")), "people": [],
            }
        extra_c = db.jload(c["extra"], {})
        groups[key]["people"].append({
            "id": c["id"], "name": c["full_name"], "role": c["role"], "email": c["email"], "cell": c["cell"] or c["tel"],
            "tel": c["tel"], "mobile": c["cell"], "company": a.get("name"), "segment": seg,
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
            "match_confidence": extra_c.get("match_confidence"), "category": extra_c.get("category"),
            "zoho": bool(c["zoho_contact_id"]),
        })
    out = {seg: [] for seg in BRANCH_ORDER}
    for g in groups.values():
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
            return {"accounts": q("SELECT count(*) FROM accounts"), "contacts": q("SELECT count(*) FROM contacts"),
                    "coverage": coverage(conn), "zoho_pulled_at": zoho.get_meta(conn, "zoho_pulled_at"),
                    "live_enabled": bool((cfg.get("zoho") or {}).get("live_enabled")),
                    "departments": list(DEPARTMENTS),
                    "relevant_departments": list((cfg.get("dashboard") or {}).get("relevant_departments") or []),
                    "last_ingest": (conn.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone() or [None])[0]}

    @app.get("/api/accounts")
    def accounts_list():
        with lock:
            return db.rows(conn, "SELECT id, name, segment, rank FROM accounts ORDER BY rank IS NULL, rank, name")

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
