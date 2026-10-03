"""v4 phase 3: the Analyze/BI view (V4-SPEC.md §4).

A fixed set of small, named SQL aggregates — no generic query builder, so every panel stays
auditable and fast on a local SQLite file. Most panels are *clickable*: each slice carries a
`filter` dict using the exact same keys the People Tree's own filter bar already understands
(`branch`, `fdept`, `fcat`, `fclass`, `frep`, `q`), so "drilldown" is just the dashboard applying
that filter and switching to the People Tree — no separate filtering mechanism to keep in sync.
A few coverage/health panels (in Zoho/in MakDB, role-pending, face/summary coverage) have no
matching filter control yet and are informational only (`filter` omitted).
"""
from __future__ import annotations

from . import db
from .images import coverage as face_coverage
from .util import ROLE_RANK, category_rank

BRANCH_LABELS = {"customer": "Customers", "competitor": "Competitors", "internal": "Internal", "supplier": "Suppliers"}


def _panel(title: str, key: str, slices: list[dict], metric: str = "count") -> dict:
    return {"title": title, "key": key, "metric": metric, "total": sum(s["value"] for s in slices), "slices": slices}


def by_branch(conn) -> dict:
    rows = db.rows(conn, "SELECT segment, count(*) n FROM contacts GROUP BY segment")
    rows.sort(key=lambda r: list(BRANCH_LABELS).index(r["segment"]) if r["segment"] in BRANCH_LABELS else 9)
    return _panel("People by branch", "branch", [
        {"label": BRANCH_LABELS.get(r["segment"], r["segment"]), "value": r["n"], "filter": {"branch": r["segment"]}}
        for r in rows])


def by_account_role(conn) -> dict:
    rows = db.rows(conn, "SELECT role, count(*) n FROM account_roles GROUP BY role ORDER BY role")
    return _panel("Accounts by role (an account can carry more than one)", "account_role", [
        {"label": f"{r['role'].capitalize()} accounts", "value": r["n"], "filter": {"branch": r["role"]}}
        for r in rows])


def allocation(conn) -> dict:
    rows = db.rows(conn, "SELECT allocated, count(*) n FROM contacts WHERE segment='customer' GROUP BY allocated")
    by_alloc = {r["allocated"]: r["n"] for r in rows}
    return _panel("Customers: allocated vs unallocated", "allocation", [
        {"label": "Allocated", "value": by_alloc.get(1, 0), "filter": {"branch": "customer", "frep": "__alloc"}},
        {"label": "Unallocated", "value": by_alloc.get(0, 0), "filter": {"branch": "customer", "frep": "__unalloc"}},
    ])


def by_category(conn) -> dict:
    rows = db.rows(conn, "SELECT category, count(*) n FROM contacts WHERE segment='customer' GROUP BY category")
    rows.sort(key=lambda r: category_rank(r["category"]))
    return _panel("Customers by call-cadence category", "category", [
        {"label": r["category"] or "(none)", "value": r["n"],
         "filter": {"branch": "customer", "fcat": r["category"] or ""}}
        for r in rows])


def by_contact_class(conn) -> dict:
    rows = db.rows(conn, "SELECT contact_class, count(*) n FROM contacts WHERE segment='customer' GROUP BY contact_class")
    order = {"engaged": 0, "lead": 1, "backlog": 2, None: 3}
    rows.sort(key=lambda r: order.get(r["contact_class"], 9))
    labels = {"engaged": "Engaged", "lead": "Lead", "backlog": "Backlog", None: "Unclassified"}
    return _panel("Customers by classification", "contact_class", [
        {"label": labels.get(r["contact_class"], r["contact_class"]), "value": r["n"],
         "filter": {"branch": "customer", "fclass": r["contact_class"] or "__none"}}
        for r in rows])


def by_department(conn) -> dict:
    rows = db.rows(conn, "SELECT department, count(*) n FROM contacts GROUP BY department ORDER BY n DESC")
    return _panel("People by department", "department", [
        {"label": r["department"] or "(unknown)", "value": r["n"], "filter": {"fdept": r["department"] or "__none"}}
        for r in rows])


def by_region(conn) -> dict:
    """Region/branch has no dedicated filter control yet — drilldown falls back to the People
    Tree's free-text search, which already matches a group's account 'branch' field."""
    rows = db.rows(conn, """SELECT coalesce(a.branch, '(none)') b, count(*) n FROM contacts c
                            JOIN accounts a ON a.id = c.account_id WHERE c.segment = 'customer'
                            GROUP BY b ORDER BY n DESC""")
    return _panel("Customers by region/branch", "region", [
        {"label": r["b"], "value": r["n"],
         "filter": {"branch": "customer", "q": r["b"]} if r["b"] != "(none)" else {"branch": "customer"}}
        for r in rows])


def sellout_by_division(conn) -> dict:
    rows = db.rows(conn, """SELECT coalesce(division, '(none)') d, sum(coalesce(latest_sellout, 0)) v
                            FROM accounts WHERE segment = 'customer' GROUP BY d ORDER BY v DESC""")
    return _panel("Latest sellout by division (R)", "sellout_division", [
        {"label": r["d"], "value": r["v"] or 0,
         "filter": {"branch": "customer", "q": r["d"]} if r["d"] != "(none)" else {"branch": "customer"}}
        for r in rows], metric="money")


def sellout_by_rep(conn) -> dict:
    rows = db.rows(conn, """SELECT coalesce(rep, '(none)') r, sum(coalesce(latest_sellout, 0)) v
                            FROM accounts WHERE segment = 'customer' GROUP BY r ORDER BY v DESC""")
    return _panel("Latest sellout by Elvey rep (R)", "sellout_rep", [
        {"label": r["r"], "value": r["v"] or 0,
         "filter": {"branch": "customer", "q": r["r"]} if r["r"] != "(none)" else {"branch": "customer"}}
        for r in rows], metric="money")


def in_zoho_coverage(conn) -> dict:
    rows = db.rows(conn, "SELECT coalesce(in_zoho, '(blank)') v, count(*) n FROM contacts GROUP BY v ORDER BY n DESC")
    return _panel("'In Zoho' flag from the source sheet", "in_zoho",
                  [{"label": r["v"], "value": r["n"]} for r in rows])


def in_makdb_coverage(conn) -> dict:
    rows = db.rows(conn, "SELECT coalesce(in_makdb, '(blank)') v, count(*) n FROM contacts GROUP BY v ORDER BY n DESC")
    return _panel("'In MakDB' flag from the source sheet", "in_makdb",
                  [{"label": r["v"], "value": r["n"]} for r in rows])


def role_coverage(conn) -> dict:
    rows = db.rows(conn, "SELECT role_source, count(*) n FROM contacts GROUP BY role_source")
    by_src = {r["role_source"]: r["n"] for r in rows}
    pending = by_src.get(None, 0) + by_src.get("pending", 0)
    labels = {"data": "From the sheet", "manual": "Manually set", "linkedin": "From LinkedIn"}
    slices = [{"label": "Role pending (blank or placeholder)", "value": pending}]
    for src in sorted(by_src, key=lambda s: -ROLE_RANK.get(s, 0)):
        if src in (None, "pending"):
            continue
        slices.append({"label": labels.get(src, src), "value": by_src[src]})
    return _panel("Where each contact's role came from", "role_source", slices)


def face_and_summary_coverage(conn) -> dict:
    cov = face_coverage(conn)
    slices = []
    for seg in ("customer", "competitor", "internal", "supplier"):
        c = cov.get(seg)
        if not c or not c["n"]:
            continue
        slices.append({"label": f"{BRANCH_LABELS[seg]}: face", "value": c["faces"]})
        slices.append({"label": f"{BRANCH_LABELS[seg]}: LinkedIn summary", "value": c["summaries"]})
    return _panel("Face + LinkedIn summary coverage", "coverage", slices)


PANELS = [by_branch, by_account_role, allocation, by_category, by_contact_class, by_department,
          by_region, sellout_by_division, sellout_by_rep, in_zoho_coverage, in_makdb_coverage,
          role_coverage, face_and_summary_coverage]


def panels(conn) -> list[dict]:
    return [p(conn) for p in PANELS]
