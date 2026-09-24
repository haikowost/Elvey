"""SQLite helpers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = Path(__file__).with_name("schema.sql")


# Columns added after the first release: existing databases are upgraded in place on connect.
MIGRATIONS = {
    "contacts": [
        ("department", "TEXT"), ("department_source", "TEXT"),
        ("employment_status", "TEXT NOT NULL DEFAULT 'unknown'"), ("linkedin_current_company", "TEXT"),
        ("moved_to_account_id", "INTEGER REFERENCES accounts(id)"),
        ("contact_status", "TEXT NOT NULL DEFAULT 'active'"), ("status_note", "TEXT"),
        ("city", "TEXT"), ("province", "TEXT"), ("country", "TEXT"), ("location_source", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # tables first (new DBs get every column), then upgrade older DBs, then indexes on new columns
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_contacts_status ON contacts(contact_status)")
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def insert(conn: sqlite3.Connection, table: str, data: dict) -> int:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
    return cur.lastrowid


def update(conn: sqlite3.Connection, table: str, row_id: int, data: dict, touch: bool = True) -> None:
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    if touch:
        sets += ", updated_at = datetime('now')"
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))


def jload(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str | None:
    if value in (None, {}, []):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
