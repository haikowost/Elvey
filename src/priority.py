"""KYC contact priority: the MD's scored order the LinkedIn harvest works through.

    python -m src.priority import                     # seed_data/kyc_priority.csv, then inputs.kyc_priority
    python -m src.priority import <file.csv|file.xlsx> [...]
    python -m src.priority stats

Input is either the flat CSV (kyc_order, contact_id, account_id, full_name, company, email, tier,
score) or the review workbook 'Elvey KYC Contact Priority <date>.xlsx'. From the workbook the
'All contacts scored' sheet supplies the scores, and Haiko's review column on the 'P1 Key — confirm'
and 'P2 Active' sheets is applied on top:

    Y             -> keep the tier, pin ahead of P1 (the four 'Key:' tiers still come first)
    N             -> demote to 'P3 Reference'
    Not relevant  -> tier 'Excluded' (never harvested)
    Correction    -> stored verbatim in contacts.kyc_correction for a human; never auto-parsed

The sheet's own tier is kept in kyc_tier_base and the review in kyc_review, so re-importing either
file in any order always gives the same effective kyc_tier (idempotent). Contacts are matched by
email first, then contact_id (the relational workbook's source id), then name + company.

Key tiers (2026-10): 'Key: Internal' (Elvey staff), 'Key: Supplier' (Milestone SA team, sub-distributors),
'Key: BD' (BD end users / consulting engineers) and 'Key: Competitor' are harvested before everything
else, in that order. Rows whose contact_id is an org-chart / vendor id that isn't in the database yet
(INT-### Elvey staff, SUP-* supplier people) are CREATED as contacts (segment internal / supplier) so
they can be harvested and appear in the People Tree.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from . import db
from .config import ROOT, load_config
from .util import as_int, clean, norm_company, norm_name

SEED_CSV = ROOT / "seed_data" / "kyc_priority.csv"
CURATED = "Internal/Competitor (curated)"
EXCLUDED = "Excluded"
DEMOTED = "P3 Reference"
REVIEW_SHEETS = ("P1 Key — confirm", "P2 Active")
SCORED_SHEET = "All contacts scored"

KEY_TIERS = ("Key: Internal", "Key: Supplier", "Key: BD", "Key: Competitor")
# harvest rank per effective tier (lower first); >= LOW_RANK is skipped unless --include-low
#   0-3 the Key tiers (in KEY_TIERS order) · 4 curated, never harvested · 5 pinned (Y) · 6 P1 · 7 P2
#   8 P3 · 9 curated re-runs and unscored · 10 P4 / Excluded
KEY_RANK = {t: i for i, t in enumerate(KEY_TIERS)}
CURATED_RANK, PINNED_RANK, OTHER_RANK = 4, 5, 9
TIER_RANK = {"P1 Key": 6, "P2 Active": 7, DEMOTED: 8}
LOW_RANK = 10
# contact_id prefixes for people the priority file knows but the relational DB doesn't
NEW_CONTACT_PREFIXES = {"INT-": "internal", "SUP-": "supplier"}


def _key(h) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(h or "").lower())


# header aliases -> field (normalised headers, so 'Score (0–100)' -> 'score0100')
ALIASES = {
    "order": ("kycorder", "order"), "contact_id": ("contactid",), "full_name": ("fullname", "name"),
    "company": ("company",), "email": ("email",), "tier": ("tier",), "score": ("score", "score0100"),
    "confirm": ("confirmynnotrelevant", "confirm", "review"),
    "correction": ("correctionnameroleemployeremail", "correction"),
    "key_reason": ("keyreason", "reason"),
}


def _rows(path: Path, sheet: str | None = None) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            raw = list(csv.DictReader(fh))
    else:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if sheet not in wb.sheetnames:
            return []
        it = wb[sheet].iter_rows(values_only=True)
        header = next(it, None) or ()
        raw = [dict(zip(header, r)) for r in it if any(v not in (None, "") for v in r)]
    out = []
    for r in raw:
        keys = {_key(k): v for k, v in r.items()}
        out.append({f: next((keys[a] for a in al if a in keys), None) for f, al in ALIASES.items()})
    return out


def normalise_review(value) -> tuple[str | None, str | None]:
    """A review cell -> (review code 'Y' | 'N' | 'Not relevant' | None, leftover text for the note).
    Anything that isn't one of the three answers is kept as a correction, never interpreted."""
    s = clean(value)
    if not s:
        return None, None
    low = s.lower().strip(" .!")
    if low in ("y", "yes", "keep", "ok"):
        return "Y", None
    if low in ("n", "no"):
        return "N", None
    if low.startswith("not rel") or low in ("nr", "irrelevant", "not relevant"):
        return "Not relevant", None
    return None, s


def effective_tier(base: str | None, review: str | None) -> str | None:
    if review == "Not relevant":
        return EXCLUDED
    if review == "N" and base in ("P1 Key", "P2 Active"):
        return DEMOTED
    return base


def harvest_rank(tier: str | None, pinned: bool = False, never_harvested: bool = True) -> int:
    """0-3 Key: Internal / Supplier / BD / Competitor · 4 curated (never harvested) · 5 pinned (Y) ·
    6 P1 · 7 P2 · 8 P3 · 9 curated re-runs and unscored · 10 P4 / Excluded (skipped unless include_low)."""
    if tier == EXCLUDED or (tier or "").startswith("P4"):
        return LOW_RANK
    if tier in KEY_RANK:
        return KEY_RANK[tier]
    if tier == CURATED:
        return CURATED_RANK if never_harvested else OTHER_RANK
    if pinned:
        return PINNED_RANK
    return TIER_RANK.get(tier, OTHER_RANK)


# the same ranking in SQL, for harvest.queue (c = contacts)
RANK_SQL = f"""CASE
    WHEN c.kyc_tier = '{EXCLUDED}' OR c.kyc_tier LIKE 'P4%' THEN {LOW_RANK}
    {" ".join(f"WHEN c.kyc_tier = '{t}' THEN {r}" for t, r in KEY_RANK.items())}
    WHEN c.kyc_tier = '{CURATED}' THEN CASE WHEN c.enrich_last_at IS NULL AND c.enrich_attempts = 0
                                       THEN {CURATED_RANK} ELSE {OTHER_RANK} END
    WHEN coalesce(c.kyc_pinned, 0) = 1 THEN {PINNED_RANK}
    WHEN c.kyc_tier = 'P1 Key' THEN {TIER_RANK["P1 Key"]}
    WHEN c.kyc_tier = 'P2 Active' THEN {TIER_RANK["P2 Active"]}
    WHEN c.kyc_tier = '{DEMOTED}' THEN {TIER_RANK[DEMOTED]}
    ELSE {OTHER_RANK} END"""


def has_priority(conn) -> bool:
    return conn.execute("SELECT 1 FROM contacts WHERE kyc_tier IS NOT NULL LIMIT 1").fetchone() is not None


class _Matcher:
    def __init__(self, conn):
        self.by_email, self.by_src, self.by_name_co, self.by_name, self.internal = {}, {}, {}, {}, {}
        for c in db.rows(conn, "SELECT c.id, c.email, c.source_id, c.name_norm, c.segment, a.name_norm AS co "
                               "FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id ORDER BY c.id"):
            if c["email"]:
                self.by_email.setdefault(c["email"].lower(), c["id"])
            if c["source_id"]:
                self.by_src.setdefault(c["source_id"], c["id"])
            self.by_name_co.setdefault((c["name_norm"], c["co"]), c["id"])
            self.by_name.setdefault(c["name_norm"], []).append(c["id"])
            if c["segment"] == "internal":
                self.internal.setdefault(c["name_norm"], c["id"])

    def add(self, cid: int, r: dict) -> None:
        if clean(r.get("contact_id")):
            self.by_src[clean(r["contact_id"])] = cid
        self.by_name_co[(norm_name(r.get("full_name")), norm_company(r.get("company")))] = cid

    def find(self, r: dict) -> int | None:
        email = (clean(r.get("email")) or "").lower()
        if email and email in self.by_email:
            return self.by_email[email]
        cid = clean(r.get("contact_id"))
        if cid and cid in self.by_src:
            return self.by_src[cid]
        name = norm_name(r.get("full_name"))
        if not name:
            return None
        hit = self.by_name_co.get((name, norm_company(r.get("company"))))
        if hit:
            return hit
        if (cid or "").upper().startswith("INT-") or r.get("tier") == "Key: Internal":
            # Elvey staff: the org-chart stubs have no account, so match them on name alone
            if name in self.internal:
                return self.internal[name]
        same = self.by_name.get(name) or []
        return same[0] if len(same) == 1 and not clean(r.get("company")) else None


def _merge_rank(tier: str | None, order: int | None) -> tuple:
    """When several scored rows resolve to one contact (the DB merged a curated seed row with the
    same person's customer row, or two contact_ids with one email), the best row wins: P1, then
    curated, then the usual harvest rank; lowest KYC order breaks ties."""
    r = harvest_rank(tier) if tier in KEY_RANK else {"P1 Key": 4.5, CURATED: 4.6}.get(tier, harvest_rank(tier) + 1)
    return (r, order if order is not None else 10**9)


def _score(v) -> float | None:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def import_file(conn, path: str | Path) -> dict:
    path = Path(path)
    m = _Matcher(conn)
    stats = {"file": str(path), "scored": 0, "matched": 0, "created": 0, "unmatched": 0, "reviewed": 0, "pinned": 0, "duplicates": 0,
             "demoted": 0, "excluded": 0, "corrections": 0, "unmatched_names": []}
    is_xlsx = path.suffix.lower() in (".xlsx", ".xlsm")
    scored = _rows(path, SCORED_SHEET) if is_xlsx else _rows(path)
    best: dict[int, tuple] = {}
    with conn:
        for r in scored:
            if not clean(r.get("tier")):
                continue
            stats["scored"] += 1
            cid = m.find(r)
            if not cid and new_contact_segment(r):
                cid = create_contact(conn, r)
                m.add(cid, r)
                stats["created"] += 1
            elif not cid:
                stats["unmatched"] += 1
                stats["unmatched_names"].append(clean(r.get("full_name")) or clean(r.get("email")) or "?")
                continue
            else:
                stats["matched"] += 1
            base = clean(r["tier"])
            rank = _merge_rank(base, as_int(r.get("order")))
            if cid in best and best[cid] <= rank:
                stats["duplicates"] += 1
                continue
            best[cid] = rank
            cur = db.one(conn, "SELECT kyc_review FROM contacts WHERE id = ?", [cid])
            db.update(conn, "contacts", cid, {
                "kyc_priority_order": as_int(r.get("order")), "kyc_score": _score(r.get("score")),
                "kyc_tier_base": base, "kyc_tier": effective_tier(base, cur["kyc_review"]),
            }, touch=False)
        if is_xlsx:
            for sheet in REVIEW_SHEETS:
                for r in _rows(path, sheet):
                    review, leftover = normalise_review(r.get("confirm"))
                    note = "; ".join(x for x in (leftover, clean(r.get("correction"))) if x) or None
                    if not review and not note:
                        continue
                    cid = m.find(r)
                    if not cid:
                        stats["unmatched"] += 1
                        stats["unmatched_names"].append(clean(r.get("full_name")) or "?")
                        continue
                    stats["reviewed"] += 1
                    c = db.one(conn, "SELECT kyc_tier_base, kyc_review FROM contacts WHERE id = ?", [cid])
                    changes: dict = {}
                    if review:
                        changes.update(kyc_review=review, kyc_pinned=1 if review == "Y" else 0,
                                       kyc_tier=effective_tier(c["kyc_tier_base"], review))
                        stats[{"Y": "pinned", "N": "demoted", "Not relevant": "excluded"}[review]] += 1
                    if note:
                        changes["kyc_correction"] = note
                        stats["corrections"] += 1
                    db.update(conn, "contacts", cid, changes, touch=False)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('kyc_priority_import', ?)",
                     [json.dumps({"file": path.name, "matched": stats["matched"], "reviewed": stats["reviewed"]})])
    stats["unmatched_names"] = stats["unmatched_names"][:25]
    return stats


def new_contact_segment(r: dict) -> str | None:
    """'internal' / 'supplier' for an INT-### / SUP-* row the database doesn't know yet, else None."""
    cid = (clean(r.get("contact_id")) or "").upper()
    if not clean(r.get("full_name")):
        return None
    return next((seg for pre, seg in NEW_CONTACT_PREFIXES.items() if cid.startswith(pre)), None)


def _account_for(conn, company: str | None, segment: str) -> int | None:
    """The account a newly created contact belongs to: an existing one with the same (loose) name,
    the internal Elvey account for staff, else (suppliers) a new supplier account."""
    from .harvest import find_account
    from .orgchart import add_account_role

    aid = find_account(conn, company) if company else None
    if not aid and segment == "internal":
        row = db.one(conn, "SELECT id FROM accounts WHERE segment = 'internal' ORDER BY rank IS NULL, rank, id LIMIT 1")
        aid = row["id"] if row else None
    if not aid and company:
        aid = db.insert(conn, "accounts", {"name": company, "name_norm": norm_company(company), "segment": segment,
                                           "source": "kyc_priority"})
    if aid:
        add_account_role(conn, aid, segment, "sheet")
    return aid


def create_contact(conn, r: dict) -> int:
    """Create a contact for an INT-### / SUP-* priority row (org chart staff, vendor teams)."""
    from .util import classify_department, split_name

    segment = new_contact_segment(r) or "customer"
    name = clean(r["full_name"])
    first, last = split_name(name)
    extra = {"source": "kyc_priority", "key_reason": clean(r.get("key_reason"))}
    return db.insert(conn, "contacts", {
        "source_id": clean(r.get("contact_id")), "account_id": _account_for(conn, clean(r.get("company")), segment),
        "full_name": name, "name_norm": norm_name(name), "first_name": first, "last_name": last,
        "email": (clean(r.get("email")) or "").lower() or None, "segment": segment,
        "role_source": "pending", "extra": db.jdump({k: v for k, v in extra.items() if v}),
        "department": classify_department(None, email=clean(r.get("email"))) if clean(r.get("email")) else None,
    })


def default_inputs(cfg) -> list[Path]:
    """seed_data/kyc_priority.csv, then the reviewed workbook from config (newest dated copy)."""
    from .ingest import latest_like

    paths = [SEED_CSV]
    p = cfg.path("inputs", "kyc_priority")
    if p:
        paths.append(latest_like(p))
    return [p for p in paths if p.exists()]


TIER_ORDER = (*KEY_TIERS, CURATED, "P1 Key", "P2 Active", DEMOTED, "P4 Low relevance", "P4 Noise / generic", EXCLUDED)


def tier_counts(conn) -> list[dict]:
    """Per effective tier: total, done (harvested), failed (no profile found / gave up), pending."""
    order = {t: i for i, t in enumerate(TIER_ORDER)}
    rows = db.rows(conn, """SELECT coalesce(kyc_tier, '(unscored)') AS tier, count(*) AS total,
                                   sum(enrich_status = 'done') AS done,
                                   sum(enrich_status IN ('no_profile', 'failed')) AS failed,
                                   sum(image_status IN ('downloaded', 'manual')) AS photo,
                                   sum(coalesce(kyc_pinned, 0)) AS pinned
                            FROM contacts WHERE contact_status = 'active' GROUP BY 1""")
    for r in rows:
        r["done"], r["photo"], r["pinned"], r["failed"] = r["done"] or 0, r["photo"] or 0, r["pinned"] or 0, r["failed"] or 0
        r["pending"] = r["total"] - r["done"] - r["failed"]
    return sorted(rows, key=lambda r: (order.get(r["tier"], 9), r["tier"]))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="load scores / tiers / review answers")
    imp.add_argument("files", nargs="*")
    sub.add_parser("stats", help="contacts per tier, harvested vs pending")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    if args.cmd == "stats":
        print(json.dumps(tier_counts(conn), indent=2))
        return
    files = [Path(f) for f in args.files] or default_inputs(cfg)
    if not files:
        print("No KYC priority file found (seed_data/kyc_priority.csv or inputs.kyc_priority) - skipped.")
        return
    for f in files:
        print(json.dumps(import_file(conn, f), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
