"""Index the KYC face library, match files to contacts, report coverage, emit to_enrich.csv.

    python -m src.images                    # match + coverage + to_enrich.csv
    python -m src.images --create-missing   # also create contacts for unmatched competitor/internal faces

Match order per file: (1) a contact already pointing at it, (2) exact expected filename,
(3) same name slug within the same segment class, (4) fuzzy name (rapidfuzz >= 92) within the
segment class, unique best only. Manually-set images (image_status='manual') are never changed.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from . import db
from .config import load_config
from .util import face_filename, norm_company, norm_name, parse_face_filename, split_name, token_segment

try:
    from rapidfuzz import fuzz

    def _score(a: str, b: str) -> float:
        return fuzz.token_sort_ratio(a, b)
except ImportError:  # pragma: no cover - fallback
    from difflib import SequenceMatcher

    def _score(a: str, b: str) -> float:
        return 100 * SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
FUZZY_MIN = 92


def index_folder(folder: Path) -> dict[str, dict]:
    """filename -> {segment, token, name_norm} for every convention-named image."""
    out = {}
    if not folder or not folder.exists():
        return out
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in IMAGE_EXT or not p.is_file():
            continue
        parsed = parse_face_filename(p.name)
        if parsed:
            token, name = parsed
            out[p.name] = {"token": token, "name_norm": norm_name(name.replace("_", " ")), "size": p.stat().st_size}
    return out


def _contacts(conn) -> list[dict]:
    return db.rows(conn, """SELECT c.*, a.name AS company FROM contacts c
                            LEFT JOIN accounts a ON a.id = c.account_id""")


def match(conn, cfg, create_missing: bool = False) -> dict:
    folder = cfg.kyc_folder
    files = index_folder(folder)
    for info in files.values():
        info["segment"] = token_segment(info["token"], cfg)
    contacts = _contacts(conn)
    by_file: dict[str, list[int]] = defaultdict(list)   # file -> contact ids
    assigned: dict[int, str] = {}

    # (1) keep existing pointers that still exist; (2) exact expected filename
    for c in contacts:
        if c["image_filename"] in files:
            assigned[c["id"]] = c["image_filename"]
            continue
        expected = face_filename(c["segment"], c["full_name"], c["company"], cfg)
        if expected in files:
            assigned[c["id"]] = expected
    # (3)/(4) name match within segment class for the rest
    for c in contacts:
        if c["id"] in assigned:
            continue
        cands = [(f, i) for f, i in files.items() if i["segment"] == c["segment"]]
        exact = [f for f, i in cands if i["name_norm"] == c["name_norm"]]
        if c["segment"] == "competitor" and len(exact) > 1:
            exact = [f for f in exact if norm_company(files[f]["token"]) in norm_company(c["company"] or "")] or exact
        if len(exact) == 1:
            assigned[c["id"]] = exact[0]
            continue
        scored = sorted(((_score(c["name_norm"], i["name_norm"]), f) for f, i in cands), reverse=True)
        if scored and scored[0][0] >= FUZZY_MIN and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            assigned[c["id"]] = scored[0][1]
    for cid, f in assigned.items():
        by_file[f].append(cid)

    created = 0
    unmatched = [f for f in files if f not in by_file]
    with conn:
        if create_missing:
            for f in list(unmatched):
                info = files[f]
                if info["segment"] == "customer":
                    continue  # customer faces need a real account; list them instead
                full = parse_face_filename(f)[1].replace("_", " ")
                acct = _seed_account(conn, info, cfg)
                first, last = split_name(full)
                cid = db.insert(conn, "contacts", {
                    "account_id": acct, "full_name": full, "name_norm": norm_name(full), "first_name": first,
                    "last_name": last or None, "segment": info["segment"], "priority": 0,
                    "image_filename": f, "image_status": "downloaded"})
                assigned[cid] = f
                by_file[f].append(cid)
                unmatched.remove(f)
                created += 1

        changed = 0
        for c in contacts:
            if c["image_status"] == "manual":
                continue
            f = assigned.get(c["id"])
            if f and (c["image_filename"] != f or c["image_status"] != "downloaded"):
                db.update(conn, "contacts", c["id"], {"image_filename": f, "image_status": "downloaded"})
                changed += 1
            elif not f and c["image_status"] == "downloaded":
                # file vanished from the library
                db.update(conn, "contacts", c["id"], {"image_filename": None, "image_status": "none"})
                changed += 1
        update_account_kyc_status(conn)

    return {"folder": str(folder), "files": len(files), "matched_files": len(by_file), "unmatched_files": unmatched,
            "contacts_updated": changed, "contacts_created": created, "coverage": coverage(conn)}


def _seed_account(conn, info: dict, cfg) -> int:
    if info["segment"] == "internal":
        name = (cfg.get("segments", {}).get("internal_accounts") or ["Elvey"])[0]
    else:
        tokens = {t.lower(): n for n, t in (cfg.get("segments", {}).get("competitor_accounts") or {}).items()}
        name = tokens.get(info["token"].lower(), info["token"])
    row = db.one(conn, "SELECT id FROM accounts WHERE name_norm = ?", [norm_company(name)])
    if row:
        return row["id"]
    return db.insert(conn, "accounts", {"name": name, "name_norm": norm_company(name), "segment": info["segment"],
                                        "source": "kyc-seed"})


def update_account_kyc_status(conn) -> None:
    """complete = every contact has face + summary; partial = some KYC captured; none = nothing yet."""
    for r in db.rows(conn, """SELECT a.id, a.kyc_status, count(c.id) n,
                                  sum(c.image_status IN ('downloaded','manual') AND c.enrich_status = 'done') full_kyc,
                                  sum(c.image_status IN ('downloaded','manual') OR c.enrich_status = 'done') any_kyc
                              FROM accounts a LEFT JOIN contacts c ON c.account_id = a.id GROUP BY a.id"""):
        status = None if not r["n"] else "complete" if r["full_kyc"] == r["n"] else "partial" if r["any_kyc"] else "none"
        if status != r["kyc_status"]:
            db.update(conn, "accounts", r["id"], {"kyc_status": status})


def coverage(conn) -> dict:
    out = {}
    for r in db.rows(conn, """SELECT segment, count(*) n,
                                  sum(image_status IN ('downloaded','manual')) faces,
                                  sum(image_status = 'no_photo') no_photo,
                                  sum(enrich_status = 'done') summaries
                              FROM contacts GROUP BY segment"""):
        out[r["segment"]] = {k: r[k] for k in ("n", "faces", "no_photo", "summaries")}
    band = db.rows(conn, """SELECT CASE WHEN priority IS NULL THEN 'unranked' WHEN priority=0 THEN 'seed'
                                WHEN priority<=50 THEN '1-50' WHEN priority<=200 THEN '51-200' ELSE '201+' END b,
                                count(*) n, sum(image_status IN ('downloaded','manual')) faces
                            FROM contacts GROUP BY b""")
    out["by_priority"] = {r["b"]: {"n": r["n"], "faces": r["faces"]} for r in band}
    return out


def to_enrich(conn, cfg, path: Path | None = None) -> tuple[Path, int]:
    """Contacts needing a face and/or LinkedIn summary, best priority first."""
    max_attempts = (cfg.get("harvest") or {}).get("max_attempts", 2)
    rows = db.rows(conn, """
        SELECT c.id, c.priority, c.segment, c.full_name, a.name AS company, c.role, c.linkedin_contact_url,
               c.image_status IN ('none') AS needs_face,
               c.enrich_status = 'pending' OR (c.enrich_status = 'failed' AND c.enrich_attempts < ?) AS needs_summary
        FROM contacts c LEFT JOIN accounts a ON a.id = c.account_id
        WHERE (c.image_status = 'none' OR c.enrich_status = 'pending'
               OR (c.enrich_status = 'failed' AND c.enrich_attempts < ?))
          AND c.enrich_status != 'no_profile'
        ORDER BY c.priority IS NULL, c.priority, c.id""", [max_attempts, max_attempts])
    path = path or cfg.path("paths", "to_enrich_csv") or cfg.data_dir / "to_enrich.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "priority", "segment", "full_name", "company", "role", "linkedin_contact_url", "needs_face",
            "needs_summary", "target_filename"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            r["target_filename"] = face_filename(r["segment"], r["full_name"], r["company"], cfg)
            w.writerow({k: r.get(k) for k in cols})
    return path, len(rows)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--create-missing", action="store_true",
                    help="create competitor/internal contacts for faces that match nobody")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    result = match(conn, cfg, args.create_missing)
    path, n = to_enrich(conn, cfg)
    result["to_enrich"] = {"path": str(path), "rows": n}
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
