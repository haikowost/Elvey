"""Deal Intelligence Graph: the single graph that every /graph view reads.

Two sources, one shape (`{nodes, links}`, see GRAPH-BRIEF in README):
  * seed  — seed_data/graph_seed.json, clearly-marked demo data;
  * live  — derived from the KYC database (accounts, contacts, account_roles, reporting lines)
            plus the optional projects / products / opportunities / graph_links tables.
Pick with `graph.source` in config.yaml or the ELVEY_GRAPH_SOURCE env var.

    python -m src.graph import deals.json   # load projects/products/opportunities/links (live mode)
    python -m src.graph stats               # what the live graph currently contains
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
from pathlib import Path

from rapidfuzz import fuzz

from . import db
from .config import load_config
from .util import norm_company, norm_name

SEED_FILE = Path(__file__).resolve().parent.parent / "seed_data" / "graph_seed.json"

RELS = ("works_at", "part_of", "does_business_with", "involved_in", "specifies", "serves", "competes",
        "opportunity", "knows")
# lens keys: a person's key is 'person', a company's is its group, projects/products their type
LENS_TYPES = ("person", "elvey", "customer", "supplier", "enduser", "consultant", "competitor", "project", "product")
GROUP_COLORS = {"elvey": "#f5a524", "customer": "#22d3ee", "enduser": "#2dd4bf", "consultant": "#e879f9",
                "competitor": "#fb5a7d", "supplier": "#60a5fa", "project": "#a78bfa", "product": "#34d399"}
# when a neighbour is reached by several relationships, the spider files it under the first of these
REL_PRIORITY = ("opportunity", "works_at", "part_of", "does_business_with", "specifies", "involved_in",
                "serves", "competes", "knows")
_SENIOR = re.compile(r"\b(ceo|cto|cfo|coo|md|managing director|director|founder|owner|head|chief|"
                     r"general manager|gm|partner|principal)\b", re.IGNORECASE)


class Graph:
    def __init__(self, nodes: list[dict], links: list[dict], source: str, details: dict | None = None):
        self.source = source
        self.by_id = {n["id"]: n for n in nodes}
        self.nodes = nodes
        self.links = [l for l in links if l["source"] in self.by_id and l["target"] in self.by_id]
        self.details = details or {}
        self.adj: dict[str, list[tuple[dict, str, str]]] = {n["id"]: [] for n in nodes}
        for l in self.links:
            self.adj[l["source"]].append((l, l["target"], "out"))
            self.adj[l["target"]].append((l, l["source"], "in"))
        for n in nodes:
            n["oppCount"] = len(all_opps(self, n["id"]))

    def neighbors(self, nid: str, rel: str | None = None) -> list[tuple[dict, str, str]]:
        return [x for x in self.adj.get(nid, []) if rel is None or x[0]["rel"] == rel]

    def payload(self) -> dict:
        return {"source": self.source, "nodes": self.nodes, "links": self.links}


def _node(**kw) -> dict:
    n = {"id": kw["id"], "label": kw["label"], "type": kw["type"], "group": kw["group"],
         "size": kw.get("size") or 1.5, "sub": kw.get("sub"), "photoUrl": kw.get("photoUrl"),
         "facts": [f for f in (kw.get("facts") or []) if f], "opps": kw.get("opps") or [],
         "region": kw.get("region"), "brands": kw.get("brands") or [],
         "zoho": kw.get("zoho"), "kyc": kw.get("kyc")}
    return n


def lens_key(n: dict) -> str:
    return "person" if n["type"] == "person" else n["group"]


# --------------------------------------------------------------------------- sources

_seed_cache: dict[str, Graph] = {}


def load_seed(path: Path = SEED_FILE) -> Graph:
    key = str(path)
    if key not in _seed_cache:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        nodes, details = [], {}
        for r in raw["nodes"]:
            details[r["id"]] = {"summary": r.get("summary"), "experience": r.get("experience") or []}
            nodes.append(_node(**r))
        _seed_cache[key] = Graph(nodes, [dict(l) for l in raw["links"]], "seed", details)
    return _seed_cache[key]


def _account_group(a: dict, roles: dict[int, list[str]]) -> str:
    if a.get("org_type") in ("enduser", "consultant"):
        return a["org_type"]
    have = roles.get(a["id"]) or [a["segment"]]
    for role, group in (("internal", "elvey"), ("competitor", "competitor"), ("customer", "customer"),
                        ("supplier", "supplier")):
        if role in have:
            return group
    return "customer"


def _split_brands(text: str | None) -> list[str]:
    return [b.strip() for b in re.split(r"[,/;|]+", text or "") if b.strip()]


def _sellout_size(v: float | None) -> float:
    return round(1.5 + min(2.0, math.log10(1 + (v or 0) / 100_000)), 2) if v else 1.5


def load_live(conn, cfg=None) -> Graph:
    accounts = db.rows(conn, "SELECT * FROM accounts")
    roles: dict[int, list[str]] = {}
    for r in db.rows(conn, "SELECT account_id, role FROM account_roles"):
        roles.setdefault(r["account_id"], []).append(r["role"])
    nodes: list[dict] = []
    links: list[dict] = []
    details: dict[str, dict] = {}
    groups = {a["id"]: _account_group(a, roles) for a in accounts}
    internal = sorted((a for a in accounts if groups[a["id"]] == "elvey"),
                      key=lambda a: (not a["name_norm"].startswith("elvey"), a["rank"] is None, a["rank"] or 0, a["id"]))
    root = f"a{internal[0]['id']}" if internal else None

    acct_opps: dict[int, list] = {}
    proj_opps: dict[int, list] = {}
    for o in db.rows(conn, "SELECT * FROM opportunities ORDER BY id"):
        opp = {k: v for k, v in (("t", o["title"]), ("p", o["stage"]), ("val", o["value"])) if v}
        if o["project_id"]:
            proj_opps.setdefault(o["project_id"], []).append(opp)
        elif o["account_id"]:
            acct_opps.setdefault(o["account_id"], []).append(opp)

    for a in accounts:
        nid, group = f"a{a['id']}", groups[a["id"]]
        sellout = a["latest_sellout"]
        extra = db.jload(a["extra"], {})
        facts = [f"Sellout: R {sellout:,.0f}" if sellout else None,
                 f"Rank #{a['rank']}" if a["rank"] else None,
                 f"Brand focus: {a['brand_focus']}" if a["brand_focus"] else None,
                 f"Elvey rep: {a['rep']}" if a["rep"] else None,
                 f"Category {extra['category']}" if extra.get("category") else None,
                 f"KYC {a['kyc_status']}" if a["kyc_status"] else None]
        nodes.append(_node(id=nid, label=a["name"], type="company", group=group,
                           size=4 if nid == root else _sellout_size(sellout),
                           sub=" · ".join(x for x in (a["division"], a["branch"]) if x) or group.capitalize(),
                           facts=facts, opps=acct_opps.get(a["id"]), region=a["cluster"],
                           brands=_split_brands(a["brand_focus"]), zoho=bool(a["zoho_account_id"])))
        if root and nid != root:
            if group == "elvey":
                links.append({"source": nid, "target": root, "rel": "part_of"})
            elif group == "customer":
                links.append({"source": root, "target": nid, "rel": "does_business_with",
                              **({"ex": {"owner": a["rep"]}} if a["rep"] else {})})
            elif group == "competitor":
                links.append({"source": nid, "target": root, "rel": "competes"})
            elif group == "supplier":
                links.append({"source": nid, "target": root, "rel": "does_business_with"})

    names = {a["id"]: a["name"] for a in accounts}
    clusters = {a["id"]: a["cluster"] for a in accounts}
    for c in db.rows(conn, "SELECT * FROM contacts WHERE contact_status = 'active'"):
        nid = f"c{c['id']}"
        acct = c["account_id"]
        group = groups.get(acct) or ("elvey" if c["segment"] == "internal" else c["segment"])
        if group not in GROUP_COLORS:
            group = "customer"
        has_photo = c["image_filename"] and c["image_status"] in ("downloaded", "manual")
        role = c["role"]
        company = names.get(acct)
        place = ", ".join(x for x in (c["city"], c["country"]) if x)
        nodes.append(_node(
            id=nid, label=c["full_name"], type="person", group=group,
            size=round(1.4 + (0.4 if c["contact_class"] == "engaged" else 0) + (0.3 if _SENIOR.search(role or "") else 0), 2),
            sub=", ".join(x for x in (role, company) if x) or None,
            photoUrl=f"/api/photo/{nid}" if has_photo else None,
            facts=[c["department"], f"Class: {c['contact_class']}" if c["contact_class"] else None,
                   f"Call cadence {c['category']}" if c["category"] else None, place or None,
                   f"Allocated to {c['allocated_rep']}" if c["allocated_rep"] and c["allocated"] else None],
            region=clusters.get(acct),
            zoho=bool(c["zoho_contact_id"]) or (c["in_zoho"] or "").upper() == "Y",
            kyc="done" if c["enrich_status"] == "done" else "pending"))
        details[nid] = {"summary": c["linkedin_summary"], "experience": db.jload(c["linkedin_experience"], []),
                        "linkedin": c["linkedin_profile_url"] or c["linkedin_contact_url"],
                        "email": c["email"], "phone": c["cell"] or c["tel"], "enrich_status": c["enrich_status"],
                        "image_filename": c["image_filename"] if has_photo else None}
        if acct:
            links.append({"source": nid, "target": f"a{acct}", "rel": "works_at"})
        elif group == "elvey" and root:
            links.append({"source": nid, "target": root, "rel": "works_at"})
        if c["reports_to_id"]:
            links.append({"source": nid, "target": f"c{c['reports_to_id']}", "rel": "knows", "ex": {"reports_to": True}})

    for p in db.rows(conn, "SELECT * FROM projects"):
        nodes.append(_node(id=f"p{p['id']}", label=p["name"], type="project", group="project", size=2,
                           sub=p["sub"], region=p["region"], brands=_split_brands(p["brands"]),
                           facts=db.jload(p["facts"], []), opps=proj_opps.get(p["id"])))
    for p in db.rows(conn, "SELECT * FROM products"):
        nodes.append(_node(id=f"pr{p['id']}", label=p["name"], type="product", group="product", size=1.8,
                           sub=p["sub"], brands=_split_brands(p["brand"])))
    for l in db.rows(conn, "SELECT * FROM graph_links"):
        links.append({"source": l["source"], "target": l["target"], "rel": l["rel"],
                      **({"ex": db.jload(l["ex"], {})} if l["ex"] else {})})
    return Graph(nodes, links, "live", details)


LIVE_CACHE_S = 10


def source_name(cfg) -> str:
    return os.environ.get("ELVEY_GRAPH_SOURCE") or (cfg.get("graph") or {}).get("source") or "live"



# --------------------------------------------------------------------------- derivations

def _brief(n: dict) -> dict:
    return {k: n.get(k) for k in ("id", "label", "type", "group", "sub", "photoUrl", "zoho", "kyc", "oppCount", "region")}


def all_opps(g: Graph, nid: str) -> list[dict]:
    """A node's own opps + `opportunity` edges + opps on the projects it's involved in (§5)."""
    n = g.by_id[nid]
    out = [{**o, "via": None} for o in n.get("opps") or []]
    for l, other, _ in g.neighbors(nid, "opportunity"):
        ex = l.get("ex") or {}
        out.append({"t": ex.get("t") or f"Opportunity: {g.by_id[other]['label']}", "p": ex.get("p"),
                    "val": ex.get("val"), "via": g.by_id[other]["label"]})
    if n["type"] != "project":
        for pid in linked_projects(g, nid):
            out += [{**o, "via": g.by_id[pid]["label"]} for o in g.by_id[pid].get("opps") or []]
    seen, uniq = set(), []
    for o in out:
        key = (o.get("t"), o.get("via"))
        if key not in seen:
            seen.add(key)
            uniq.append(o)
    return uniq


def linked_projects(g: Graph, nid: str) -> list[str]:
    return list(dict.fromkeys(o for l, o, _ in g.neighbors(nid)
                              if l["rel"] in ("involved_in", "specifies", "opportunity") and g.by_id[o]["type"] == "project"))


def _via_projects(g: Graph, nid: str, projects: list[str], rels: tuple, groups: tuple) -> list[dict]:
    """Nodes in `groups` attached by one of `rels` to any of `projects`, with the shared project names."""
    found: dict[str, list[str]] = {}
    for pid in projects:
        for l, other, _ in g.neighbors(pid):
            if other != nid and l["rel"] in rels and g.by_id[other]["group"] in groups:
                found.setdefault(other, []).append(g.by_id[pid]["label"])
    return [{**_brief(g.by_id[o]), "via": ", ".join(dict.fromkeys(v))} for o, v in found.items()]


def investigate(g: Graph, aid: str) -> dict:
    n = g.by_id[aid]
    reps = [{**_brief(g.by_id[o]), "kyc": g.by_id[o].get("kyc")}
            for l, o, d in g.neighbors(aid, "works_at") if d == "in"]
    projects = linked_projects(g, aid)
    consultants = _via_projects(g, aid, projects, ("specifies",), ("consultant",))
    endusers = _via_projects(g, aid, projects, ("involved_in",), ("enduser",))
    have = {e["id"] for e in endusers}
    for l, o, d in g.neighbors(aid, "serves"):
        if d == "out" and o not in have and g.by_id[o]["group"] == "enduser":
            endusers.append({**_brief(g.by_id[o]), "via": "serves"})
    competitors = _via_projects(g, aid, projects, ("involved_in", "opportunity"), ("competitor",))
    owner = next(((l.get("ex") or {}).get("owner") for l, o, d in g.neighbors(aid, "does_business_with")
                  if d == "in" and g.by_id[o]["group"] == "elvey" and (l.get("ex") or {}).get("owner")), None)
    opps = all_opps(g, aid)
    proj_rows = [{**_brief(g.by_id[p]), "brands": g.by_id[p].get("brands") or [],
                  "opps": g.by_id[p].get("opps") or []} for p in projects]
    return {
        "account": {**_brief(n), "facts": n["facts"], "brands": n.get("brands") or []},
        "owner": owner,
        "stats": {"contacts": len(reps), "projects": len(projects), "consultants": len(consultants),
                  "endusers": len(endusers), "opportunities": len(opps)},
        "contacts": reps, "opportunities": opps, "projects": proj_rows, "consultants": consultants,
        "endusers": endusers, "competitors": competitors,
        "plays": suggested_plays(g, n, reps, consultants, endusers, competitors, owner),
    }


def suggested_plays(g, n, reps, consultants, endusers, competitors, owner) -> list[str]:
    plays = []
    for c in consultants:
        plays.append(f"Get in front of {c['label']} — they specify {c['via']}.")
    for c in competitors:
        plays.append(f"Competitive threat: {c['label']} is also on {c['via']}.")
    for e in endusers:
        if not e.get("zoho"):
            plays.append(f"Add end-user {e['label']} to Zoho (linked via {e['via']}).")
    pending = [r["label"] for r in reps if r.get("kyc") != "done"]
    if pending:
        plays.append(f"Run KYC on {', '.join(pending[:3])}{' and others' if len(pending) > 3 else ''}.")
    if not owner and n["group"] not in ("elvey", "competitor"):
        plays.append(f"No Elvey account owner recorded for {n['label']} — assign one.")
    if not reps:
        plays.append(f"No contacts captured at {n['label']} yet.")
    return plays[:6]


def ego(g: Graph, cid: str, depth: int = 1) -> dict:
    """The centre + its neighbours, grouped by relationship; depth=2 adds a faint outer ring."""
    ring1: dict[str, dict] = {}
    for l, o, d in g.neighbors(cid):
        e = ring1.setdefault(o, {**_brief(g.by_id[o]), "rels": [], "ring": 1})
        e["rels"].append(l["rel"])
        if l["rel"] == "opportunity":
            e["opp"] = (l.get("ex") or {}).get("t")
    for e in ring1.values():
        e["rels"] = sorted(set(e["rels"]), key=REL_PRIORITY.index)
        e["rel"] = e["rels"][0]
        e["via"] = None
    groups: dict[str, list[str]] = {}
    for oid, e in ring1.items():
        groups.setdefault(e["rel"], []).append(oid)
    ring2: dict[str, dict] = {}
    if depth >= 2:
        for parent in ring1:
            for l, o, _ in g.neighbors(parent):
                if o != cid and o not in ring1 and o not in ring2:
                    ring2[o] = {**_brief(g.by_id[o]), "ring": 2, "rel": l["rel"], "parent": parent}
    keep = {cid, *ring1, *ring2}
    links = [l for l in g.links if l["source"] in keep and l["target"] in keep
             and (cid in (l["source"], l["target"]) or (depth >= 2 and (l["source"] in ring2 or l["target"] in ring2)))]
    return {"center": {**_brief(g.by_id[cid]), "facts": g.by_id[cid]["facts"]},
            "groups": [{"rel": r, "ids": groups[r]} for r in REL_PRIORITY if r in groups],
            "nodes": list(ring1.values()) + list(ring2.values()), "links": links}


def dossier(g: Graph, nid: str) -> dict:
    n = g.by_id[nid]
    rel_groups: dict[str, list] = {}
    for l, o, d in g.neighbors(nid):
        via = None
        if l["rel"] in ("specifies", "involved_in") and g.by_id[o]["type"] != "project":
            shared = set(linked_projects(g, nid)) & set(linked_projects(g, o))
            via = ", ".join(sorted(g.by_id[p]["label"] for p in shared)) or None
        rel_groups.setdefault(l["rel"], []).append({**_brief(g.by_id[o]), "dir": d, "via": via, "ex": l.get("ex")})
    det = dict(g.details.get(nid) or {})
    det.pop("image_filename", None)
    employer = next((o for l, o, d in g.neighbors(nid, "works_at") if d == "out"), None)
    return {"node": n, "employer": g.by_id[employer]["label"] if employer else None, "employer_id": employer,
            "enrichment": det,
            "kyc_pending": n["type"] == "person" and not (det.get("summary") or det.get("experience")),
            "relationships": [{"rel": r, "items": rel_groups[r]} for r in RELS if r in rel_groups],
            "opportunities": all_opps(g, nid),
            "investigable": n["type"] == "company"}


def search(g: Graph, q: str, limit: int = 20) -> list[dict]:
    q = (q or "").strip().lower()
    if not q:
        return []
    scored = []
    for n in g.nodes:
        label = n["label"].lower()
        if label == q:
            s = 100
        elif label.startswith(q):
            s = 90
        elif any(w.startswith(q) for w in re.split(r"[\s\-/]+", label)):
            s = 80
        elif q in label:
            s = 65
        elif q in (n.get("sub") or "").lower():
            s = 50
        else:
            s = fuzz.partial_ratio(q, label) if len(q) >= 3 else 0
            s = s * 0.6 if s >= 75 else 0
        if s:
            scored.append((s + min(n["size"], 4), n))
    scored.sort(key=lambda x: (-x[0], x[1]["label"]))
    return [_brief(n) for _, n in scored[:limit]]


def filter_graph(g: Graph, types=None, rels=None, regions=None, brands=None, opps_only=False) -> dict:
    types, rels, regions, brands = (set(x or []) for x in (types, rels, regions, brands))

    def ok(n):
        return ((not types or lens_key(n) in types) and (not regions or n.get("region") in regions)
                and (not brands or brands & set(n.get("brands") or [])) and (not opps_only or n["oppCount"]))
    keep = {n["id"] for n in g.nodes if ok(n)}
    return {"source": g.source, "nodes": [n for n in g.nodes if n["id"] in keep],
            "links": [l for l in g.links if l["source"] in keep and l["target"] in keep and (not rels or l["rel"] in rels)]}


def monogram_svg(n: dict) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", n["label"]) if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    color = GROUP_COLORS.get(n["group"], "#8fa3c8")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
            f'<circle cx="64" cy="64" r="60" fill="#0b1326" stroke="{color}" stroke-width="6"/>'
            f'<text x="64" y="64" dy=".35em" text-anchor="middle" font-family="Inter,Arial,sans-serif" '
            f'font-size="{52 if len(initials) < 2 else 46}" font-weight="700" fill="{color}">{html.escape(initials)}</text></svg>')


# --------------------------------------------------------------------------- import (live mode)

def _resolve(ref: str, keys: dict[str, str]) -> str | None:
    """Map an import-file node id to a DB node id ('a12', 'c5', 'p3', 'pr2')."""
    return keys.get(ref) or (ref if re.fullmatch(r"(a|c|p|pr)\d+", ref) else None)


def import_file(conn, path: str | Path) -> dict:
    """Load projects/products/opportunities/links from a seed-shaped JSON file. Companies and people
    are matched to EXISTING accounts/contacts by name (the KYC ingest owns those); projects and
    products are created or updated by their file id. Idempotent."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    keys: dict[str, str] = {}
    stats = {"projects": 0, "products": 0, "opportunities": 0, "links": 0, "unmatched": []}
    with conn:
        for n in raw.get("nodes") or []:
            fid = n["id"]
            if n["type"] == "company":
                a = db.one(conn, "SELECT id FROM accounts WHERE name_norm = ?", [norm_company(n["label"])])
                if a:
                    keys[fid] = f"a{a['id']}"
                    if n.get("group") in ("enduser", "consultant"):
                        conn.execute("UPDATE accounts SET org_type = ? WHERE id = ?", [n["group"], a["id"]])
                else:
                    stats["unmatched"].append(n["label"])
            elif n["type"] == "person":
                c = db.one(conn, "SELECT id FROM contacts WHERE name_norm = ?", [norm_name(n["label"])])
                if c:
                    keys[fid] = f"c{c['id']}"
                else:
                    stats["unmatched"].append(n["label"])
            elif n["type"] == "project":
                vals = {"name": n["label"], "sub": n.get("sub"), "region": n.get("region"),
                        "brands": ", ".join(n.get("brands") or []) or None, "facts": db.jdump(n.get("facts"))}
                conn.execute("INSERT INTO projects (name, sub, region, brands, facts, source_key) VALUES (?,?,?,?,?,?) "
                             "ON CONFLICT (source_key) DO UPDATE SET name=excluded.name, sub=excluded.sub, "
                             "region=excluded.region, brands=excluded.brands, facts=excluded.facts",
                             [*vals.values(), fid])
                pid = db.one(conn, "SELECT id FROM projects WHERE source_key = ?", [fid])["id"]
                keys[fid] = f"p{pid}"
                stats["projects"] += 1
            elif n["type"] == "product":
                conn.execute("INSERT INTO products (name, sub, brand, source_key) VALUES (?,?,?,?) "
                             "ON CONFLICT (source_key) DO UPDATE SET name=excluded.name, sub=excluded.sub, brand=excluded.brand",
                             [n["label"], n.get("sub"), ", ".join(n.get("brands") or []) or None, fid])
                keys[fid] = f"pr{db.one(conn, 'SELECT id FROM products WHERE source_key = ?', [fid])['id']}"
                stats["products"] += 1
        for n in raw.get("nodes") or []:
            target = keys.get(n["id"])
            if not target or target[0] not in "ap" or target.startswith("pr"):
                continue
            for i, o in enumerate(n.get("opps") or []):
                col = "project_id" if target.startswith("p") else "account_id"
                conn.execute(f"INSERT INTO opportunities (title, stage, value, {col}, source_key) VALUES (?,?,?,?,?) "
                             "ON CONFLICT (source_key) DO UPDATE SET title=excluded.title, stage=excluded.stage, value=excluded.value",
                             [o.get("t"), o.get("p"), o.get("val"), int(target.lstrip("ap")), f"{n['id']}#{i}"])
                stats["opportunities"] += 1
        for l in raw.get("links") or []:
            s, t = _resolve(l["source"], keys), _resolve(l["target"], keys)
            if not s or not t or l["rel"] not in RELS or l["rel"] == "works_at":
                continue  # employment comes from the KYC data itself, never from this file
            conn.execute("INSERT INTO graph_links (source, target, rel, ex) VALUES (?,?,?,?) "
                         "ON CONFLICT (source, target, rel) DO UPDATE SET ex=excluded.ex",
                         [s, t, l["rel"], db.jdump(l.get("ex"))])
            stats["links"] += 1
    stats["unmatched"] = sorted(set(stats["unmatched"]))
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="load projects/products/opportunities/links from a JSON file")
    imp.add_argument("file")
    sub.add_parser("stats", help="count what the live graph contains")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    if args.cmd == "import":
        s = import_file(conn, args.file)
        print(f"projects {s['projects']} · products {s['products']} · opportunities {s['opportunities']} · links {s['links']}")
        if s["unmatched"]:
            print(f"Not matched to an existing account/contact (skipped, with their links): {', '.join(s['unmatched'])}")
    else:
        g = load_live(conn, cfg)
        counts: dict[str, int] = {}
        for n in g.nodes:
            counts[lens_key(n)] = counts.get(lens_key(n), 0) + 1
        print(json.dumps({"nodes": counts, "links": len(g.links)}, indent=2))


if __name__ == "__main__":
    main()
