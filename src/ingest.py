"""Load the Customer Data Consolidation output into the local DB (accounts + contacts).

    python -m src.ingest                      # path from config.yaml inputs.workbook
    python -m src.ingest --file other.xlsx    # override
    python -m src.ingest --all                # ignore top-50 / top-500 scope

Re-runnable: rows are matched (source id -> accno/email -> normalised name) and updated in
place. Source values never blank out existing data, and enrichment / Zoho columns are never
touched by ingest.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from . import db
from .config import load_config
from .util import (as_int, clean, linkedin_url, norm_company, norm_name, parse_face_filename,
                   parse_money, segment_of, split_name)

# Flat-file header aliases (normalised: lowercase, non-alnum removed).
FLAT_ALIASES: dict[str, list[str]] = {
    "company": ["company", "account", "accountname", "customer", "customername", "companyname"],
    "accno": ["accno", "accountno", "accountnumber", "accnum", "custno"],
    "full_name": ["fullname", "contactname", "name", "contact"],
    "first_name": ["firstname", "first"],
    "last_name": ["lastname", "surname", "last"],
    "role": ["role", "title", "jobtitle", "position", "designation"],
    "email": ["email", "emailaddress", "mail"],
    "tel": ["tel", "phone", "telephone", "officephone", "work"],
    "cell": ["cell", "mobile", "cellphone", "mobilephone"],
    "rep": ["rep", "am", "amcode", "accountmanager", "salesrep", "allocationproposed"],
    "division": ["division"],
    "cluster": ["cluster"],
    "branch": ["branch"],
    "latest_sellout": ["fy26sellout", "fy26", "sellout", "latestsellout", "fy25sellout"],
    "brand_focus": ["brandfocus", "brands", "brand"],
    "allocation": ["allocation", "allocationstatus"],
    "kyc_status": ["kycstatus", "kyc"],
    "linkedin_contact_url": ["linkedinurl", "linkedin", "linkedincontacturl", "linkedinprofile"],
    "linkedin_company_url": ["linkedincompanyurl", "companylinkedin"],
    "segment": ["segment", "branchtype", "peopletreebranch"],
    "rank": ["rank", "priority", "contactrank"],
    "account_rank": ["accountrank"],
    "image_filename": ["imagefilename", "image", "face"],
}

ACCOUNT_FIELDS = ("name", "accno", "segment", "division", "cluster", "branch", "rep", "latest_sellout",
                  "brand_focus", "allocation", "kyc_status", "linkedin_company_url", "source")
CONTACT_FIELDS = ("full_name", "first_name", "last_name", "role", "email", "tel", "cell",
                  "linkedin_contact_url", "segment", "org_group")
AUTHORITATIVE = ("rank", "priority")  # ordering is owned by the source, None is allowed to clear


# --------------------------------------------------------------------------- readers

def read_table(path: Path, sheet: str | None = None) -> list[dict]:
    """Rows of a CSV or one xlsx sheet as dicts keyed by header."""
    if path.suffix.lower() in (".csv", ".txt"):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    return _sheet_rows(ws)


def _sheet_rows(ws) -> list[dict]:
    it = ws.iter_rows(values_only=True)
    header = next(it, None)
    if not header:
        return []
    keys = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(header)]
    return [dict(zip(keys, r)) for r in it if any(v not in (None, "") for v in r)]


def read_workbook(path: Path) -> dict[str, list[dict]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return {ws.title: _sheet_rows(ws) for ws in wb.worksheets if not ws.title.lower().startswith("read me")}


# --------------------------------------------------------------------------- relational

def load_relational(sheets: dict[str, list[dict]], cfg: dict, all_rows: bool = False) -> tuple[list[dict], list[dict]]:
    scope = cfg.get("scope") or {}
    top_a = None if all_rows else scope.get("top_accounts")
    top_c = None if all_rows else scope.get("top_contacts")
    seg_cfg = cfg.get("segments") or {}
    internal_names = {norm_company(n) for n in seg_cfg.get("internal_accounts") or []}
    competitor_names = {norm_company(n): n for n in (seg_cfg.get("competitor_accounts") or {})}
    token_to_competitor = {t.lower(): n for n, t in (seg_cfg.get("competitor_accounts") or {}).items()}

    reps = {clean(r.get("am_code")): r for r in sheets.get("dim_reps", []) if clean(r.get("am_code"))}
    reps_by_name = {norm_name(r.get("name")): r for r in reps.values() if clean(r.get("name"))}
    acct_rank = {clean(r["account_id"]): as_int(r.get("rank")) for r in sheets.get("view_top50_projects", [])}
    contact_rank = {clean(r["contact_id"]): as_int(r.get("rank")) for r in sheets.get("view_top500_contacts", [])}
    brands = {clean(b["brand_id"]): clean(b.get("brand")) for b in sheets.get("dim_brands", [])}

    sellout_by_acct: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for s in sheets.get("fact_sellout", []):
        value = (parse_money(s.get("fy26")) or 0) or (parse_money(s.get("fy25")) or 0)
        if value > 0 and clean(s.get("account_id")):
            sellout_by_acct[clean(s["account_id"])].append((value, brands.get(clean(s.get("brand_id"))) or s.get("brand_id")))

    raw_accounts = {clean(a["account_id"]): a for a in sheets.get("dim_accounts", []) if clean(a.get("account_id"))}
    by_norm = {norm_company(a.get("company")): aid for aid, a in raw_accounts.items()}

    def account_segment(name: str | None) -> str:
        n = norm_company(name)
        if n in internal_names:
            return "internal"
        if n in competitor_names:
            return "competitor"
        return "customer"

    # roles known anywhere in the sheet (seed rows are often blank while a duplicate row has it)
    role_by_name: dict[str, str] = {}
    for c in sheets.get("dim_contacts", []):
        if clean(c.get("role")):
            role_by_name.setdefault(norm_name(c.get("full_name")), clean(c["role"]))

    # ---- contacts in scope
    contacts: list[dict] = []
    wanted_accounts: set[str] = set()
    synthetic_accounts: dict[str, dict] = {}
    for c in sheets.get("dim_contacts", []):
        cid = clean(c.get("contact_id"))
        seg = segment_of(c.get("segment"))
        rank = contact_rank.get(cid)
        if seg == "customer":
            in_scope = top_c is None or (rank is not None and rank <= top_c) or \
                (bool(scope.get("include_kyc_faces", True)) and bool(clean(c.get("image_filename"))))
        else:
            in_scope = bool(scope.get("include_competitor_internal", True))
        full_name = clean(c.get("full_name")) or " ".join(filter(None, [clean(c.get("first_name")), clean(c.get("last_name"))]))
        if not in_scope or not full_name:
            continue
        aid = clean(c.get("account_id"))
        if not aid:  # competitor / internal seeds carry no account: derive from face token or config
            aid = _seed_account_id(c, seg, seg_cfg, token_to_competitor, by_norm, synthetic_accounts)
        acct_name = (raw_accounts.get(aid) or synthetic_accounts.get(aid) or {}).get("company")
        acct_seg = account_segment(acct_name)
        if acct_seg != "customer":
            seg = acct_seg  # e.g. Elvey staff listed as customers of the 'Elvey' account
        first, last = clean(c.get("first_name")), clean(c.get("last_name"))
        if not first:
            first, last = split_name(full_name)
        role = clean(c.get("role")) or (role_by_name.get(norm_name(full_name)) if seg != "customer" else None)
        org_group = None
        if seg == "internal":
            rep = reps_by_name.get(norm_name(full_name))
            if rep:
                role = role or clean(rep.get("role"))
                org_group = clean(rep.get("branch")) or clean(rep.get("cluster"))
        contacts.append({
            "source_id": cid, "account_source_id": aid, "full_name": full_name,
            "first_name": first, "last_name": last or None, "role": role,
            "email": (clean(c.get("email")) or "").lower() or None,
            "tel": clean(c.get("tel")), "cell": clean(c.get("cell")),
            "linkedin_contact_url": linkedin_url(c.get("linkedin_url")),
            "segment": seg, "org_group": org_group,
            "priority": rank if seg == "customer" else 0,
            "image_filename": clean(c.get("image_filename")),
            "extra": _compact({"category": clean(c.get("category")), "role_status": clean(c.get("role_status")),
                               "src": clean(c.get("src"))}),
        })
        wanted_accounts.add(aid)

    # ---- accounts in scope
    for aid, r in acct_rank.items():
        if top_a is None or (r is not None and r <= top_a):
            wanted_accounts.add(aid)
    if top_a is None and top_c is None:
        wanted_accounts.update(raw_accounts)

    accounts: list[dict] = []
    for aid in sorted(filter(None, wanted_accounts)):
        a = raw_accounts.get(aid) or synthetic_accounts.get(aid)
        if not a:
            continue
        code = clean(a.get("allocation_proposed")) or clean(a.get("am_code"))
        rep = clean((reps.get(code) or {}).get("name")) or code
        conflict = (clean(a.get("allocation_conflict")) or "").upper() == "Y"
        allocation = clean(a.get("allocation_status"))
        if conflict:
            allocation = f"Conflict: {clean(a.get('allocation_candidates')) or code}"
        top_brands = [b for _, b in sorted(sellout_by_acct.get(aid, []), reverse=True)[:3]]
        rank = acct_rank.get(aid)
        accounts.append({
            "source_id": aid, "name": clean(a.get("company")), "accno": clean(a.get("accno")),
            "segment": account_segment(a.get("company")),
            "division": clean(a.get("division")), "cluster": clean(a.get("cluster")),
            "branch": clean(a.get("branch")), "rep": rep,
            "latest_sellout": parse_money(a.get("fy26_sellout")) or parse_money(a.get("fy25_sellout")),
            "brand_focus": ", ".join(top_brands) or None, "allocation": allocation,
            "rank": rank if (top_a is None or (rank is not None and rank <= top_a)) else None,
            "source": "consolidation",
            "extra": _compact({k: clean(a.get(k)) for k in (
                "category", "axis_partner", "milestone_tier", "installer_category", "tasha_visit",
                "tasha_concerns", "allocation_conflict", "allocation_candidates", "am_code", "am_codes_all",
                "quoted_by", "in_zoho", "zoho_owner", "managed", "project_flag")}
                | {"fy25_sellout": parse_money(a.get("fy25_sellout")), "fy26_sellout": parse_money(a.get("fy26_sellout"))}),
        })
    return accounts, contacts


def _seed_account_id(c: dict, seg: str, seg_cfg: dict, token_to_competitor: dict, by_norm: dict,
                     synthetic: dict) -> str:
    if seg == "internal":
        name = (seg_cfg.get("internal_accounts") or ["Elvey"])[0]
    else:
        parsed = parse_face_filename(clean(c.get("image_filename")) or "")
        token = parsed[0] if parsed else "Competitor"
        name = token_to_competitor.get(token.lower(), token)
    aid = by_norm.get(norm_company(name))
    if aid:
        return aid
    aid = f"SEED:{norm_company(name)}"
    synthetic.setdefault(aid, {"account_id": aid, "company": name})
    return aid


# --------------------------------------------------------------------------- flat

def _hkey(h: str) -> str:
    return "".join(ch for ch in str(h).lower() if ch.isalnum())


def load_flat(rows: list[dict], cfg: dict, all_rows: bool = False) -> tuple[list[dict], list[dict]]:
    if not rows:
        return [], []
    headers = {_hkey(h): h for h in rows[0]}
    colmap = {field: next((headers[a] for a in aliases if a in headers), None) for field, aliases in FLAT_ALIASES.items()}
    get = lambda r, f: r.get(colmap[f]) if colmap.get(f) else None  # noqa: E731
    top_c = None if all_rows else (cfg.get("scope") or {}).get("top_contacts")

    accounts: dict[str, dict] = {}
    contacts: list[dict] = []
    for i, r in enumerate(rows, 1):
        company = clean(get(r, "company"))
        full = clean(get(r, "full_name")) or " ".join(filter(None, [clean(get(r, "first_name")), clean(get(r, "last_name"))]))
        rank = as_int(get(r, "rank")) or i
        if not company or not full or (top_c is not None and rank > top_c):
            continue
        key = f"FLAT:{norm_company(company)}"
        acct = accounts.setdefault(key, {"source_id": key, "name": company, "segment": segment_of(get(r, "segment")),
                                         "source": "flat", "extra": {}})
        for f in ("accno", "division", "cluster", "branch", "rep", "brand_focus", "allocation", "kyc_status"):
            acct[f] = acct.get(f) or clean(get(r, f))
        acct["latest_sellout"] = acct.get("latest_sellout") or parse_money(get(r, "latest_sellout"))
        acct["linkedin_company_url"] = acct.get("linkedin_company_url") or linkedin_url(get(r, "linkedin_company_url"))
        acct["rank"] = acct.get("rank") or as_int(get(r, "account_rank"))
        first, last = clean(get(r, "first_name")), clean(get(r, "last_name"))
        if not first:
            first, last = split_name(full)
        contacts.append({
            "source_id": None, "account_source_id": key, "full_name": full, "first_name": first,
            "last_name": last or None, "role": clean(get(r, "role")),
            "email": (clean(get(r, "email")) or "").lower() or None,
            "tel": clean(get(r, "tel")), "cell": clean(get(r, "cell")),
            "linkedin_contact_url": linkedin_url(get(r, "linkedin_contact_url")),
            "segment": segment_of(get(r, "segment")), "org_group": None, "priority": rank,
            "image_filename": clean(get(r, "image_filename")), "extra": None,
        })
    return list(accounts.values()), contacts


# --------------------------------------------------------------------------- upsert

def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "")}


def _merge(existing: dict, incoming: dict, fields: Iterable[str]) -> dict:
    """Changes to apply: non-empty incoming values that differ; authoritative keys may clear."""
    out = {}
    for f in fields:
        if f not in incoming:
            continue
        new = incoming[f]
        if f in AUTHORITATIVE:
            if existing.get(f) != new:
                out[f] = new
        elif new not in (None, "") and existing.get(f) != new:
            out[f] = new
    if "extra" in incoming:
        merged = {**db.jload(existing.get("extra"), {}), **(incoming.get("extra") or {})}
        if db.jdump(merged) != existing.get("extra"):
            out["extra"] = db.jdump(merged)
    return out


def _fold(into: dict, other: dict) -> None:
    """Merge a duplicate source record: first non-empty value wins, best (lowest) priority wins."""
    for k, v in other.items():
        if k == "extra":
            into["extra"] = {**(v or {}), **(into.get("extra") or {})}
        elif k in ("priority", "rank"):
            vals = [x for x in (into.get(k), v) if x is not None]
            into[k] = min(vals) if vals else None
        elif into.get(k) in (None, "") and v not in (None, ""):
            into[k] = v


def dedupe(accounts: list[dict], contacts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Collapse duplicate source rows so one DB row receives exactly one merged record (idempotency)."""
    canon: dict[str, str] = {}
    merged_a: dict[str, dict] = {}
    for a in accounts:
        if not a.get("name"):
            continue
        key = f"accno:{a['accno']}" if a.get("accno") else f"name:{norm_company(a['name'])}"
        if key in merged_a:
            _fold(merged_a[key], a)
        else:
            merged_a[key] = dict(a)
        canon[a["source_id"]] = merged_a[key]["source_id"]
    merged_c: dict[tuple, dict] = {}
    by_name: dict[tuple, tuple] = {}
    for c in contacts:
        c = {**c, "account_source_id": canon.get(c.get("account_source_id"), c.get("account_source_id"))}
        name_key = ("name", c["account_source_id"], norm_name(c["full_name"]))
        key = ("email", c["email"]) if c.get("email") else by_name.get(name_key, name_key)
        if key not in merged_c and name_key in by_name:
            key = by_name[name_key]
        if key in merged_c:
            _fold(merged_c[key], c)
        else:
            merged_c[key] = c
        by_name.setdefault(name_key, key)
    return list(merged_a.values()), list(merged_c.values())


def upsert(conn, accounts: list[dict], contacts: list[dict]) -> dict:
    accounts, contacts = dedupe(accounts, contacts)
    stats = Counter()
    acct_ids: dict[str, int] = {}
    with conn:
        for a in accounts:
            if not a.get("name"):
                continue
            a = {**a, "name_norm": norm_company(a["name"])}
            existing = (db.one(conn, "SELECT * FROM accounts WHERE source_id = ?", [a["source_id"]]) if a.get("source_id") else None) \
                or (db.one(conn, "SELECT * FROM accounts WHERE accno = ?", [a["accno"]]) if a.get("accno") else None) \
                or db.one(conn, "SELECT * FROM accounts WHERE name_norm = ? ORDER BY id LIMIT 1", [a["name_norm"]])
            if existing:
                changes = _merge(existing, a, (*ACCOUNT_FIELDS, "name_norm", "rank"))
                if a.get("source_id") and not existing.get("source_id"):
                    changes["source_id"] = a["source_id"]
                db.update(conn, "accounts", existing["id"], changes)
                stats["accounts_updated" if changes else "accounts_unchanged"] += 1
                acct_ids[a["source_id"]] = existing["id"]
            else:
                row = {k: a.get(k) for k in (*ACCOUNT_FIELDS, "source_id", "name_norm", "rank")}
                row["extra"] = db.jdump(a.get("extra"))
                acct_ids[a["source_id"]] = db.insert(conn, "accounts", row)
                stats["accounts_created"] += 1

        for c in contacts:
            account_id = acct_ids.get(c.get("account_source_id"))
            c = {**c, "name_norm": norm_name(c["full_name"]), "account_id": account_id}
            existing = (db.one(conn, "SELECT * FROM contacts WHERE source_id = ?", [c["source_id"]]) if c.get("source_id") else None) \
                or (db.one(conn, "SELECT * FROM contacts WHERE lower(email) = ?", [c["email"]]) if c.get("email") else None) \
                or db.one(conn, "SELECT * FROM contacts WHERE account_id IS ? AND name_norm = ? ORDER BY id LIMIT 1",
                          [account_id, c["name_norm"]])
            if existing:
                changes = _merge(existing, c, (*CONTACT_FIELDS, "name_norm", "account_id"))
                # a duplicate source row must not demote an already-ranked contact
                if c.get("priority") is not None and (existing.get("priority") is None or c["priority"] < existing["priority"]):
                    changes["priority"] = c["priority"]
                if c.get("source_id") and not existing.get("source_id"):
                    changes["source_id"] = c["source_id"]
                if c.get("image_filename") and not existing.get("image_filename"):
                    changes["image_filename"] = c["image_filename"]
                db.update(conn, "contacts", existing["id"], changes)
                stats["contacts_updated" if changes else "contacts_unchanged"] += 1
            else:
                row = {k: c.get(k) for k in (*CONTACT_FIELDS, "source_id", "name_norm", "account_id", "priority", "image_filename")}
                row["extra"] = db.jdump(c.get("extra"))
                db.insert(conn, "contacts", row)
                stats["contacts_created"] += 1
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_ingest', datetime('now'))")
    return dict(stats)


def summary(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "accounts": q("SELECT count(*) FROM accounts"),
        "ranked_accounts": q("SELECT count(*) FROM accounts WHERE rank IS NOT NULL"),
        "contacts": q("SELECT count(*) FROM contacts"),
        "with_email": q("SELECT count(*) FROM contacts WHERE email IS NOT NULL"),
        "with_linkedin_url": q("SELECT count(*) FROM contacts WHERE linkedin_contact_url IS NOT NULL"),
        "with_face": q("SELECT count(*) FROM contacts WHERE image_status IN ('downloaded','manual')"),
        "by_segment": dict(conn.execute("SELECT segment, count(*) FROM contacts GROUP BY segment").fetchall()),
        "by_division": dict(conn.execute(
            "SELECT coalesce(a.division,'(none)'), count(*) FROM contacts c LEFT JOIN accounts a ON a.id=c.account_id "
            "WHERE c.segment='customer' GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "by_priority_band": dict(conn.execute(
            "SELECT CASE WHEN priority IS NULL THEN 'unranked' WHEN priority = 0 THEN 'seed (comp/int)' "
            "WHEN priority <= 100 THEN '1-100' WHEN priority <= 250 THEN '101-250' WHEN priority <= 500 THEN '251-500' "
            "ELSE '500+' END AS band, count(*) FROM contacts GROUP BY band").fetchall()),
    }


def run(cfg, file: str | None = None, all_rows: bool = False) -> dict:
    path = Path(file) if file else cfg.path("inputs", "workbook")
    if not path or not path.exists():
        raise SystemExit(f"Input not found: {path}\nSet inputs.workbook in config.yaml (or KYC_WORKBOOK / --file).")
    fmt = (cfg.get("inputs") or {}).get("format", "auto")
    sheets = read_workbook(path) if path.suffix.lower() in (".xlsx", ".xlsm") else {}
    if fmt == "relational" or (fmt == "auto" and "dim_contacts" in sheets):
        accounts, contacts = load_relational(sheets, cfg, all_rows)
    else:
        rows = read_table(path, (cfg.get("inputs") or {}).get("sheet"))
        accounts, contacts = load_flat(rows, cfg, all_rows)
    conn = db.connect(cfg.db_path)
    stats = upsert(conn, accounts, contacts)
    return {"input": str(path), "loaded": {"accounts": len(accounts), "contacts": len(contacts)}, "upsert": stats,
            "db": summary(conn)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="consolidation output (xlsx/csv); default config inputs.workbook")
    ap.add_argument("--all", action="store_true", help="load every row, ignoring scope.top_* limits")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    print(json.dumps(run(load_config(args.config), args.file, args.all), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
