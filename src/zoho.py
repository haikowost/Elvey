"""Zoho CRM review-and-select sync.

    python -m src.zoho check                  # auth + which mapped custom fields are missing
    python -m src.zoho pull                   # read Accounts + Contacts into the local mirror (never writes to Zoho)
    python -m src.zoho diff [--status new]    # new / changed / in-sync per record
    python -m src.zoho push --select new      # DRY RUN of every 'new' record (default)
    python -m src.zoho push --select file.json --live   # live push of a saved selection

The dashboard's "Zoho sync" tab drives the same functions (pull, diff, push selected).
Safety: pushes are dry runs unless --live (CLI) / live mode (UI) is chosen AND
config zoho.live_enabled is true. Local empty values never blank out Zoho data.
Every write is recorded in zoho_sync_log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from . import db
from .config import load_config
from .util import norm_company, norm_name

ACCOUNTS_HOSTS = {
    "com": "https://accounts.zoho.com", "eu": "https://accounts.zoho.eu", "in": "https://accounts.zoho.in",
    "com.au": "https://accounts.zoho.com.au", "au": "https://accounts.zoho.com.au", "jp": "https://accounts.zoho.jp",
    "ca": "https://accounts.zohocloud.ca", "sa": "https://accounts.zoho.sa", "com.cn": "https://accounts.zoho.com.cn",
    "cn": "https://accounts.zoho.com.cn", "uk": "https://accounts.zoho.uk",
}
MODULE = {"account": "Accounts", "contact": "Contacts"}
# Zoho standard field lengths (custom single-line = 255).
MAX_LEN = {"First_Name": 40, "Last_Name": 80, "Title": 100, "Email": 100, "Phone": 50, "Mobile": 30, "Department": 50,
           "Account_Name": 200}
NUMERIC = {"Latest_Sellout", "Rank"}
PHONE = {"Phone", "Mobile"}
ALWAYS_PULL = {"Accounts": ["id", "Account_Name", "Modified_Time"],
               "Contacts": ["id", "Full_Name", "First_Name", "Last_Name", "Email", "Account_Name", "Record_Image",
                            "Modified_Time"]}


class ZohoError(RuntimeError):
    pass


# --------------------------------------------------------------------------- API client

class ZohoClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, dc: str = "com",
                 api_version: str = "v3", pause_s: float = 0.3, session: requests.Session | None = None):
        if not (client_id and client_secret and refresh_token):
            raise ZohoError("Zoho credentials missing: set ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN in .env")
        dc = (dc or "com").lower().lstrip(".")
        if dc not in ACCOUNTS_HOSTS:
            raise ZohoError(f"Unknown ZOHO_DC '{dc}' (use one of {', '.join(sorted(ACCOUNTS_HOSTS))})")
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.accounts_host = ACCOUNTS_HOSTS[dc]
        self.api_domain = "https://www.zohoapis." + ("com.au" if dc == "au" else "com.cn" if dc == "cn" else dc)
        if dc == "ca":
            self.api_domain = "https://www.zohoapis.ca"
        self.api_version = api_version
        self.pause_s = pause_s
        self.http = session or requests.Session()
        self._token: str | None = None
        self._token_exp = 0.0

    @classmethod
    def from_env(cls, cfg) -> "ZohoClient":
        z = cfg.get("zoho") or {}
        return cls(os.environ.get("ZOHO_CLIENT_ID", ""), os.environ.get("ZOHO_CLIENT_SECRET", ""),
                   os.environ.get("ZOHO_REFRESH_TOKEN", ""), os.environ.get("ZOHO_DC", z.get("dc", "com")),
                   z.get("api_version", "v3"), float(z.get("pause_s", 0.3)))

    def _refresh(self) -> None:
        r = self.http.post(f"{self.accounts_host}/oauth/v2/token", params={
            "refresh_token": self.refresh_token, "client_id": self.client_id,
            "client_secret": self.client_secret, "grant_type": "refresh_token"}, timeout=30)
        body = r.json() if r.content else {}
        if "access_token" not in body:
            raise ZohoError(f"Token refresh failed ({r.status_code}): {body.get('error') or r.text[:200]} — "
                            f"check credentials and ZOHO_DC (currently {self.accounts_host})")
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600)) - 120
        if body.get("api_domain"):
            self.api_domain = body["api_domain"]

    def request(self, method: str, path: str, **kw) -> requests.Response:
        """Authenticated call with token refresh, 429/5xx backoff and a polite pause."""
        url = path if path.startswith("http") else f"{self.api_domain}/crm/{self.api_version}/{path.lstrip('/')}"
        for attempt in range(6):
            if not self._token or time.time() > self._token_exp:
                self._refresh()
            headers = {**kw.pop("headers", {}), "Authorization": f"Zoho-oauthtoken {self._token}"}
            r = self.http.request(method, url, headers=headers, timeout=60, **kw)
            time.sleep(self.pause_s)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue
            return r
        raise ZohoError(f"{method} {url} kept failing ({r.status_code}): {r.text[:300]}")

    def field_names(self, module: str) -> list[str]:
        r = self.request("GET", "settings/fields", params={"module": module})
        if r.status_code != 200:
            raise ZohoError(f"fields({module}) {r.status_code}: {r.text[:300]}")
        return [f["api_name"] for f in r.json().get("fields", [])]

    def iter_records(self, module: str, fields: Iterable[str], per_page: int = 200) -> Iterable[dict]:
        fields = list(dict.fromkeys(fields))[:50]  # v3 accepts max 50 fields per call
        params: dict[str, Any] = {"fields": ",".join(fields), "per_page": per_page, "page": 1}
        while True:
            r = self.request("GET", module, params=params)
            if r.status_code == 204:
                return
            if r.status_code != 200:
                raise ZohoError(f"GET {module} {r.status_code}: {r.text[:300]}")
            body = r.json()
            yield from body.get("data", [])
            info = body.get("info", {})
            if not info.get("more_records"):
                return
            if info.get("next_page_token"):  # required beyond 2,000 records
                params = {"fields": params["fields"], "per_page": per_page, "page_token": info["next_page_token"]}
            else:
                params["page"] = params.get("page", 1) + 1

    def write(self, method: str, module: str, records: list[dict]) -> list[dict]:
        r = self.request(method, module, json={"data": records, "trigger": []})
        try:
            data = r.json().get("data")
        except ValueError:
            data = None
        if not data:
            raise ZohoError(f"{method} {module} {r.status_code}: {r.text[:300]}")
        return data

    def upload_photo(self, module: str, record_id: str, content: bytes, filename: str) -> tuple[bool, str]:
        r = self.request("POST", f"{module}/{record_id}/photo", files={"file": (filename, content, "image/jpeg")})
        try:
            body = r.json()
        except ValueError:
            body = {}
        ok = r.status_code in (200, 201) and str(body.get("status", "success")).lower() == "success"
        return ok, body.get("message") or body.get("code") or r.text[:200]


# --------------------------------------------------------------------------- mapping

def _fieldmap(cfg, entity: str) -> dict[str, str]:
    return dict((cfg.get("zoho") or {}).get(f"{entity}s_fields") or {})


def _value(zfield: str, v: Any) -> Any:
    if v is None or v == "":
        return None
    if zfield == "Rank":
        return int(v)
    if zfield in NUMERIC:
        return round(float(v), 2)
    s = str(v).strip()
    if zfield in MAX_LEN:
        s = s[: MAX_LEN[zfield]]
    return s or None


def local_values(cfg, entity: str, rec: dict) -> dict[str, dict]:
    """zoho_field -> {local, value} for one local record (after Zoho-side formatting)."""
    rec = dict(rec)
    if entity == "contact" and not rec.get("last_name"):
        # Last_Name is mandatory in Zoho: single-name contacts go in as Last_Name only
        rec["first_name"], rec["last_name"] = None, rec.get("full_name")
    return {zf: {"local": local, "value": _value(zf, rec.get(local))} for local, zf in _fieldmap(cfg, entity).items()}


def _cmp(zfield: str, v: Any) -> Any:
    if v in (None, "", [], {}):
        return None
    if isinstance(v, dict):
        return v.get("id") or v.get("name")
    if zfield in NUMERIC:
        try:
            return round(float(str(v).replace(",", "")), 2)
        except ValueError:
            return str(v)
    if zfield in PHONE:
        d = re.sub(r"\D", "", str(v))
        return "0" + d[2:] if d.startswith("27") and len(d) == 11 else d
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s.lower() if zfield == "Email" else s


def same(zfield: str, a: Any, b: Any) -> bool:
    return _cmp(zfield, a) == _cmp(zfield, b)


# --------------------------------------------------------------------------- mirror + diff

def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
    return db.jload(row[0], default) if row else default


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", [key, json.dumps(value)])


def pull(conn, cfg, client: ZohoClient, log=print) -> dict:
    """Read Zoho into zoho_records. Read-only against Zoho."""
    z = cfg.get("zoho") or {}
    out = {}
    for entity, module in MODULE.items():
        available = client.field_names(module)
        wanted = ALWAYS_PULL[module] + [f for f in _fieldmap(cfg, entity).values() if f in available]
        records = list(client.iter_records(module, wanted, int(z.get("per_page", 200))))
        with conn:
            conn.execute("DELETE FROM zoho_records WHERE module = ?", [module])
            conn.executemany("INSERT INTO zoho_records(module, zoho_id, data) VALUES (?,?,?)",
                             [(module, str(r["id"]), json.dumps(r, ensure_ascii=False)) for r in records])
            set_meta(conn, f"zoho_fields:{module}", available)
            set_meta(conn, "zoho_pulled_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        out[module] = len(records)
        log(f"pulled {len(records)} {module}")
    return out


def missing_fields(conn, cfg) -> dict[str, list[str]]:
    out = {}
    for entity, module in MODULE.items():
        available = get_meta(conn, f"zoho_fields:{module}")
        if available is None:
            continue
        out[module] = [zf for zf in _fieldmap(cfg, entity).values() if zf not in available]
    return out


def _snapshot(conn, module: str) -> dict[str, dict]:
    return {r["zoho_id"]: json.loads(r["data"]) for r in db.rows(conn, "SELECT zoho_id, data FROM zoho_records WHERE module = ?", [module])}


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _build_item(entity: str, rec: dict, zrec: dict | None, match_by: str | None, cfg, available: set | None) -> dict:
    fields = []
    for zf, lv in local_values(cfg, entity, rec).items():
        zv = (zrec or {}).get(zf)
        if zf == "Account_Name" and isinstance(zv, dict):
            zv = zv.get("name")
        fields.append({"local": lv["local"], "zoho_field": zf, "local_value": lv["value"], "zoho_value": zv,
                       "available": available is None or zf in available,
                       # never propose blanking Zoho: only non-empty local values count
                       "differs": lv["value"] is not None and not same(zf, lv["value"], zv)})
    pushable = [f for f in fields if f["available"] and f["differs"]]
    status = "new" if zrec is None else ("changed" if pushable else "in-sync")
    return {"entity": entity, "local_id": rec["id"], "name": rec.get("name") or rec.get("full_name"),
            "zoho_id": str(zrec["id"]) if zrec else None, "match_by": match_by, "status": status, "fields": fields,
            "linked": bool(rec.get(f"zoho_{entity}_id")), "last_synced": rec.get("zoho_last_synced")}


def compute_diff(conn, cfg) -> dict:
    kyc = cfg.kyc_folder
    snap_a, snap_c = _snapshot(conn, "Accounts"), _snapshot(conn, "Contacts")
    avail_a = get_meta(conn, "zoho_fields:Accounts")
    avail_c = get_meta(conn, "zoho_fields:Contacts")
    avail_a, avail_c = (set(avail_a) if avail_a else None), (set(avail_c) if avail_c else None)
    accno_field = _fieldmap(cfg, "account").get("accno")

    a_by_accno, a_by_name = {}, {}
    for zid, r in snap_a.items():
        if accno_field and r.get(accno_field):
            a_by_accno.setdefault(str(r[accno_field]).strip(), zid)
        a_by_name.setdefault(norm_company(r.get("Account_Name")), []).append(zid)

    accounts = []
    local_to_zoho_acct: dict[int, str] = {}
    for a in db.rows(conn, "SELECT * FROM accounts ORDER BY rank IS NULL, rank, name"):
        zid, how = None, None
        if a["zoho_account_id"] and a["zoho_account_id"] in snap_a:
            zid, how = a["zoho_account_id"], "zoho id"
        elif a["accno"] and a["accno"] in a_by_accno:
            zid, how = a_by_accno[a["accno"]], "account no"
        elif a_by_name.get(a["name_norm"]):
            cands = a_by_name[a["name_norm"]]
            zid, how = cands[0], "name" + (f" (ambiguous: {len(cands)} in Zoho)" if len(cands) > 1 else "")
        item = _build_item("account", a, snap_a.get(zid), how, cfg, avail_a)
        item.update({"segment": a["segment"], "rank": a["rank"], "division": a["division"]})
        if a["zoho_account_id"] and not zid:
            item["note"] = "stored Zoho id no longer exists in Zoho"
        accounts.append(item)
        if zid:
            local_to_zoho_acct[a["id"]] = zid

    c_by_email, c_by_acct_name, c_by_name = {}, {}, {}
    for zid, r in snap_c.items():
        if r.get("Email"):
            c_by_email.setdefault(r["Email"].strip().lower(), zid)
        full = r.get("Full_Name") or " ".join(filter(None, [r.get("First_Name"), r.get("Last_Name")]))
        acct = (r.get("Account_Name") or {}).get("id") if isinstance(r.get("Account_Name"), dict) else None
        c_by_acct_name.setdefault((acct, norm_name(full)), zid)
        c_by_name.setdefault(norm_name(full), []).append((zid, acct))

    contacts = []
    # people who left / aren't relevant are only shown if they are already linked to a Zoho record
    for c in db.rows(conn, """SELECT c.*, a.name AS company FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
                              WHERE c.contact_status = 'active' OR c.zoho_contact_id IS NOT NULL
                              ORDER BY c.priority IS NULL, c.priority, c.full_name"""):
        zid, how = None, None
        zacct = local_to_zoho_acct.get(c["account_id"])
        if c["zoho_contact_id"] and c["zoho_contact_id"] in snap_c:
            zid, how = c["zoho_contact_id"], "zoho id"
        elif c["email"] and c["email"].lower() in c_by_email:
            zid, how = c_by_email[c["email"].lower()], "email"
        elif zacct and (zacct, c["name_norm"]) in c_by_acct_name:
            zid, how = c_by_acct_name[(zacct, c["name_norm"])], "account + name"
        else:  # name only when unique and the Zoho contact has no account to contradict it
            cands = [z for z, acc in c_by_name.get(c["name_norm"], []) if acc is None]
            if len(c_by_name.get(c["name_norm"], [])) == 1 and cands:
                zid, how = cands[0], "name (no account in Zoho)"
        zrec = snap_c.get(zid)
        item = _build_item("contact", c, zrec, how, cfg, avail_c)
        img = kyc / c["image_filename"] if c["image_filename"] and kyc else None
        local_hash = _file_hash(img) if img else None
        zoho_has = bool((zrec or {}).get("Record_Image"))
        pending = bool(local_hash) and local_hash != c["zoho_photo_hash"] and (not zoho_has or bool(c["zoho_photo_hash"]))
        item["photo"] = {"local": c["image_filename"] if local_hash else None, "zoho_has": zoho_has, "pending": pending}
        zoho_acct_of_contact = ((zrec or {}).get("Account_Name") or {}).get("id") if isinstance((zrec or {}).get("Account_Name"), dict) else None
        item["account_link"] = {"local_account_id": c["account_id"], "zoho_account_id": zacct,
                                "differs": bool(zrec) and bool(zacct) and zoho_acct_of_contact != zacct}
        if item["status"] == "in-sync" and (pending or item["account_link"]["differs"]):
            item["status"] = "changed"
        item.update({"company": c["company"], "segment": c["segment"], "priority": c["priority"]})
        contacts.append(item)

    def counts(items):
        out = {"new": 0, "changed": 0, "in-sync": 0}
        for i in items:
            out[i["status"]] += 1
        return out

    return {"pulled_at": get_meta(conn, "zoho_pulled_at"), "missing_fields": missing_fields(conn, cfg),
            "summary": {"accounts": counts(accounts), "contacts": counts(contacts)},
            "accounts": accounts, "contacts": contacts}


# --------------------------------------------------------------------------- push

def _log(conn, entity, local_id, zoho_id, action, fields, result) -> None:
    conn.execute("INSERT INTO zoho_sync_log(entity, local_id, zoho_id, action, fields_pushed, result) VALUES (?,?,?,?,?,?)",
                 [entity, local_id, zoho_id, action, db.jdump(fields), result])


def _merge_snapshot(conn, module: str, zoho_id: str, payload: dict) -> None:
    row = conn.execute("SELECT data FROM zoho_records WHERE module = ? AND zoho_id = ?", [module, zoho_id]).fetchone()
    data = json.loads(row[0]) if row else {"id": zoho_id}
    data.update(payload)
    if module == "Contacts":
        data["Full_Name"] = " ".join(filter(None, [data.get("First_Name"), data.get("Last_Name")]))
    conn.execute("INSERT OR REPLACE INTO zoho_records(module, zoho_id, data, pulled_at) VALUES (?,?,?,datetime('now'))",
                 [module, zoho_id, json.dumps(data, ensure_ascii=False)])


def plan_push(conn, cfg, selections: list[dict]) -> list[dict]:
    """Turn selections into ordered operations. Pure (no writes).

    selection = {"entity": "account"|"contact", "local_id": int,
                 "fields": [local column names] | None (= every pushable field), "photo": bool}
    """
    diff = compute_diff(conn, cfg)
    items = {(i["entity"], i["local_id"]): i for i in diff["accounts"] + diff["contacts"]}
    sel = {(s["entity"], int(s["local_id"])): s for s in selections}

    # a new contact needs its account in Zoho first: include the parent account automatically
    for (entity, lid), s in list(sel.items()):
        it = items.get((entity, lid))
        if entity == "contact" and it and it["status"] == "new":
            parent = it["account_link"]["local_account_id"]
            pit = items.get(("account", parent))
            if pit and pit["status"] == "new" and ("account", parent) not in sel:
                sel[("account", parent)] = {"entity": "account", "local_id": parent, "fields": None, "auto": True}

    ops = []
    for entity in ("account", "contact"):
        for (e, lid), s in sel.items():
            if e != entity or (e, lid) not in items:
                continue
            it = items[(e, lid)]
            wanted = None if s.get("fields") is None else set(s["fields"])  # [] = no fields
            payload = {f["zoho_field"]: f["local_value"] for f in it["fields"]
                       if f["available"] and f["local_value"] is not None
                       and (it["status"] == "new" or f["differs"])
                       and (wanted is None or f["local"] in wanted)}
            if entity == "contact":
                link = it["account_link"]
                if link["zoho_account_id"] and (it["status"] == "new" or link["differs"]):
                    payload["Account_Name"] = {"id": link["zoho_account_id"]}
                elif it["status"] == "new" and ("account", link["local_account_id"]) in sel:
                    payload["Account_Name"] = {"pending_local_account": link["local_account_id"]}
            if it["status"] == "new":
                action = "create"
            elif payload:
                action = "update"
            else:
                action = "link"  # already in sync: just store the Zoho id locally
            ops.append({"entity": entity, "local_id": lid, "name": it["name"], "action": action,
                        "zoho_id": it["zoho_id"], "payload": payload, "auto": bool(s.get("auto")),
                        "photo": bool(s.get("photo", entity == "contact" and it.get("photo", {}).get("pending")))
                        and bool((it.get("photo") or {}).get("local"))})
    return ops


def push(conn, cfg, client: ZohoClient | None, selections: list[dict], dry_run: bool = True, log=print) -> dict:
    ops = plan_push(conn, cfg, selections)
    if dry_run:
        return {"dry_run": True, "operations": ops}
    if not (cfg.get("zoho") or {}).get("live_enabled"):
        raise PermissionError("Live Zoho push is disabled: review a dry run, then set zoho.live_enabled: true in config.yaml")
    if client is None:
        raise ZohoError("no Zoho client")
    batch = int((cfg.get("zoho") or {}).get("batch_size", 100))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    results = []
    local_acct_zoho: dict[int, str] = {}

    for entity in ("account", "contact"):
        module, table, idcol = MODULE[entity], f"{entity}s", f"zoho_{entity}_id"
        mine = [o for o in ops if o["entity"] == entity]
        for o in mine:  # resolve accounts created a moment ago
            acct = o["payload"].get("Account_Name")
            if isinstance(acct, dict) and "pending_local_account" in acct:
                zid = local_acct_zoho.get(acct["pending_local_account"])
                if zid:
                    o["payload"]["Account_Name"] = {"id": zid}
                else:
                    o["payload"].pop("Account_Name")
        for action, method in (("create", "POST"), ("update", "PUT")):
            todo = [o for o in mine if o["action"] == action]
            for i in range(0, len(todo), batch):
                chunk = todo[i:i + batch]
                records = [{**o["payload"], **({"id": o["zoho_id"]} if action == "update" else {})} for o in chunk]
                try:
                    answers = client.write(method, module, records)
                except ZohoError as e:
                    answers = [{"code": "ERROR", "message": str(e)}] * len(chunk)
                with conn:
                    for o, ans in zip(chunk, answers):
                        ok = ans.get("code") == "SUCCESS"
                        zid = str((ans.get("details") or {}).get("id") or o["zoho_id"] or "") or None
                        if ok:
                            o["zoho_id"] = zid
                            db.update(conn, table, o["local_id"], {idcol: zid, "zoho_last_synced": now})
                            _merge_snapshot(conn, module, zid, o["payload"])
                        _log(conn, entity, o["local_id"], zid, action, o["payload"],
                             "ok" if ok else f"error:{ans.get('code')}:{ans.get('message')}:{json.dumps(ans.get('details'))[:300]}")
                        results.append({"entity": entity, "local_id": o["local_id"], "name": o["name"], "action": action,
                                        "zoho_id": zid, "ok": ok, "message": ans.get("message")})
                        if entity == "account" and ok:
                            local_acct_zoho[o["local_id"]] = zid
        with conn:
            for o in mine:
                if o["action"] == "link":
                    db.update(conn, table, o["local_id"], {idcol: o["zoho_id"], "zoho_last_synced": now})
                    _log(conn, entity, o["local_id"], o["zoho_id"], "link", None, "ok")
                    results.append({"entity": entity, "local_id": o["local_id"], "name": o["name"], "action": "link",
                                    "zoho_id": o["zoho_id"], "ok": True})
                    if entity == "account":
                        local_acct_zoho[o["local_id"]] = o["zoho_id"]

    # photos last, once every contact has a Zoho id
    for o in ops:
        if o["entity"] != "contact" or not o["photo"] or not o["zoho_id"]:
            continue
        c = db.one(conn, "SELECT image_filename FROM contacts WHERE id = ?", [o["local_id"]])
        path = cfg.kyc_folder / c["image_filename"]
        content = path.read_bytes()
        ok, msg = client.upload_photo("Contacts", o["zoho_id"], content, path.name)
        with conn:
            if ok:
                db.update(conn, "contacts", o["local_id"], {"zoho_photo_hash": hashlib.sha1(content).hexdigest()})
                _merge_snapshot(conn, "Contacts", o["zoho_id"], {"Record_Image": "uploaded"})
            _log(conn, "contact", o["local_id"], o["zoho_id"], "photo", {"file": path.name}, "ok" if ok else f"error:{msg}")
        results.append({"entity": "contact", "local_id": o["local_id"], "name": o["name"], "action": "photo",
                        "zoho_id": o["zoho_id"], "ok": ok, "message": msg})
    return {"dry_run": False, "operations": ops, "results": results,
            "ok": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)}


def select(diff: dict, what: str, entity: str | None = None, ids: list[int] | None = None) -> list[dict]:
    """Build selections from a diff: what = new | changed | pending (new+changed)."""
    statuses = {"new": {"new"}, "changed": {"changed"}, "pending": {"new", "changed"}}[what]
    out = []
    for key in ("accounts", "contacts"):
        for it in diff[key]:
            if (entity and it["entity"] != entity) or it["status"] not in statuses or (ids and it["local_id"] not in ids):
                continue
            out.append({"entity": it["entity"], "local_id": it["local_id"], "fields": None})
    return out


def setup_steps(missing: dict[str, list[str]], cfg) -> str:
    types = (cfg.get("zoho") or {}).get("custom_field_types") or {}
    lines = []
    for module, fields in missing.items():
        if not fields:
            continue
        lines.append(f"\n{module}: Setup (gear) → Customization → Modules and Fields → {module} → Standard layout, then add:")
        for f in fields:
            label = f.replace("_", " ")
            lines.append(f"  - '{types.get(f, 'Single Line')}' field labelled \"{label}\"  (API name must be {f})")
        lines.append("  Save the layout. Verify API names under Setup → Developer Hub → APIs & SDKs → API Names.")
    return "\n".join(lines) if lines else "All mapped fields exist in Zoho."


# --------------------------------------------------------------------------- CLI

def _print_diff(diff: dict, status: str | None, limit: int) -> None:
    print(f"Zoho mirror pulled at: {diff['pulled_at'] or 'never (run pull)'}")
    print("summary:", json.dumps(diff["summary"]))
    for key in ("accounts", "contacts"):
        shown = [i for i in diff[key] if not status or i["status"] == status][:limit]
        print(f"\n{key} ({len(shown)} shown)")
        for i in shown:
            changes = ", ".join(f["zoho_field"] for f in i["fields"] if f["differs"] and f["available"])
            photo = " +photo" if (i.get("photo") or {}).get("pending") else ""
            print(f"  [{i['status']:<7}] #{i['local_id']:<5} {i['name'][:40]:<40} zoho={i['zoho_id'] or '-':<20} "
                  f"{('by ' + i['match_by']) if i['match_by'] else ''} {changes}{photo}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "pull", "diff", "push", "log"])
    ap.add_argument("--status", choices=["new", "changed", "in-sync"])
    ap.add_argument("--select", default="new", help="new | changed | pending | path/to/selection.json")
    ap.add_argument("--entity", choices=["account", "contact"])
    ap.add_argument("--ids", help="comma-separated local ids (with --select new/changed/pending)")
    ap.add_argument("--live", action="store_true", help="really write to Zoho (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)

    if args.command == "diff":
        _print_diff(compute_diff(conn, cfg), args.status, args.limit)
        return
    if args.command == "log":
        for r in db.rows(conn, "SELECT * FROM zoho_sync_log ORDER BY id DESC LIMIT ?", [args.limit]):
            print(f"{r['ts']}  {r['entity']:<7} #{r['local_id']:<5} {r['action']:<6} {r['zoho_id'] or '-':<20} {r['result']}")
        return

    client = ZohoClient.from_env(cfg) if (args.command != "push" or args.live) else None
    if args.command == "check":
        with conn:
            for module in MODULE.values():
                set_meta(conn, f"zoho_fields:{module}", client.field_names(module))
        print(f"auth OK → {client.api_domain}")
        print(setup_steps(missing_fields(conn, cfg), cfg))
        return
    if args.command == "pull":
        print(json.dumps(pull(conn, cfg, client)))
        d = compute_diff(conn, cfg)
        print("diff:", json.dumps(d["summary"]))
        if any(d["missing_fields"].values()):
            print("Missing Zoho fields (skipped on push):", json.dumps(d["missing_fields"]))
        return

    # push
    if os.path.exists(args.select):
        selections = json.loads(Path(args.select).read_text(encoding="utf-8"))
    else:
        ids = [int(x) for x in args.ids.split(",")] if args.ids else None
        selections = select(compute_diff(conn, cfg), args.select, args.entity, ids)
    if not selections:
        print("Nothing selected.")
        return
    ops = plan_push(conn, cfg, selections)
    counts: dict[str, int] = {}
    for o in ops:
        k = f"{o['entity']}:{o['action']}"
        counts[k] = counts.get(k, 0) + 1
    print(json.dumps(ops[: args.limit], indent=2, ensure_ascii=False))
    print("plan:", json.dumps(counts), "photos:", sum(o["photo"] for o in ops))
    if not args.live:
        print("\nDRY RUN — nothing written. Re-run with --live to push.")
        return
    if not args.yes:
        if not sys.stdin.isatty() or input(f"\nPush {len(ops)} operations to Zoho LIVE? type 'yes': ").strip() != "yes":
            print("Aborted.")
            return
    result = push(conn, cfg, client, selections, dry_run=False)
    for r in result["results"]:
        print(f"  {'OK ' if r['ok'] else 'ERR'} {r['entity']:<7} {r['action']:<6} {r['name']} {r.get('zoho_id') or ''} "
              f"{'' if r['ok'] else r.get('message')}")
    print(f"done: {result['ok']} ok, {result['failed']} failed")


if __name__ == "__main__":
    main()
