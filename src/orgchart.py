"""v4 phase 1: multi-role accounts (account_roles) and the one-time Elvey org-chart seed for
contact-level reporting lines (contacts.reports_to_id). See V4-SPEC.md sections 1 and 3.

    python -m src.orgchart roles    # sync account_roles from accounts.segment + sheet hints
    python -m src.orgchart seed     # one-time reports_to_id import from seed_data/
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import db
from .config import load_config
from .util import ACCOUNT_ROLES, classify_department, infer_supplier_from_title, norm_name, split_name

SEED_FILE = Path(__file__).resolve().parent.parent / "seed_data" / "elvey_org_chart.json"


# --------------------------------------------------------------------------- account_roles

def add_account_role(conn, account_id: int, role: str, source: str) -> None:
    if role not in ACCOUNT_ROLES:
        raise ValueError(f"unknown account role '{role}'")
    conn.execute(
        "INSERT INTO account_roles (account_id, role, source) VALUES (?, ?, ?) "
        "ON CONFLICT (account_id, role) DO NOTHING",
        (account_id, role, source),
    )


def sync_account_roles(conn) -> dict:
    """Ensure every account's primary `segment` is reflected in account_roles, then layer in the
    sheet-hint precedence step for `supplier` (Zoho's own account-type pull isn't wired up yet —
    that's the first, still-missing step; this covers the second). Idempotent: existing rows are
    never removed or overwritten, only filled in where blank (ON CONFLICT DO NOTHING)."""
    accounts = db.rows(conn, "SELECT id, segment, extra FROM accounts")
    primary = supplier = 0
    with conn:
        for a in accounts:
            add_account_role(conn, a["id"], a["segment"], "primary")
            primary += 1
            extra = db.jload(a["extra"], {})
            if a["segment"] != "supplier" and any(extra.get(k) for k in
                                                   ("axis_partner", "milestone_partner", "installer_category")):
                add_account_role(conn, a["id"], "supplier", "sheet")
                supplier += 1
    return {"accounts": len(accounts), "primary_roles": primary, "supplier_roles_inferred": supplier}


def roles_for_account(conn, account_id: int) -> list[str]:
    return [r["role"] for r in db.rows(
        conn, "SELECT role FROM account_roles WHERE account_id = ? ORDER BY role", [account_id])]


# --------------------------------------------------------------------------- org-chart seed

def _load_seed() -> list[dict]:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def seed_reports_to(conn, seed: list[dict] | None = None) -> dict:
    """One-time import of the Elvey org chart (seed_data/elvey_org_chart.json) as the initial
    `reports_to_id` chain for Internal-branch contacts. Processed top-down (every node's manager
    already appears earlier in the list) so each report links straight to its manager's row.
    Matches by name against existing Internal contacts; creates a stub contact for anyone not
    already in the database. Safe to re-run: matching is deterministic, so a repeat run links the
    same people the same way instead of duplicating them."""
    seed = seed if seed is not None else _load_seed()
    created = matched = linked = 0
    contact_id_by_key: dict[str, int] = {}
    with conn:
        for node in seed:
            name = node["name"]
            key = norm_name(name)
            existing = db.one(
                conn, "SELECT id FROM contacts WHERE segment = 'internal' AND name_norm = ?", [key])
            if existing:
                contact_id = existing["id"]
                matched += 1
            else:
                first, last = split_name(name)
                contact_id = db.insert(conn, "contacts", {
                    "full_name": name, "name_norm": key, "first_name": first, "last_name": last,
                    "role": node.get("title"), "role_source": "manual", "segment": "internal",
                    "department": classify_department(node.get("title")), "department_source": "role",
                    "contact_status": "left" if node.get("left") else "active",
                })
                created += 1
            contact_id_by_key[key] = contact_id
            reports_to_name = node.get("reports_to")
            if reports_to_name:
                manager_id = contact_id_by_key.get(norm_name(reports_to_name))
                if manager_id and manager_id != contact_id:
                    db.update(conn, "contacts", contact_id, {"reports_to_id": manager_id}, touch=False)
                    linked += 1
    return {"nodes": len(seed), "matched": matched, "created": created, "linked": linked}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("roles", "seed"))
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    try:
        result: dict[str, Any] = sync_account_roles(conn) if args.action == "roles" else seed_reports_to(conn)
    finally:
        conn.close()
    print(result)


if __name__ == "__main__":
    main()
