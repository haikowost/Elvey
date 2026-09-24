"""Naming convention, normalisation and parsing helpers shared by every module."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

SEGMENTS = ("competitor", "internal", "customer")

_COMPANY_SUFFIXES = {
    "pty", "ltd", "limited", "proprietary", "cc", "inc", "llc", "plc", "npc", "soc",
    "co", "company", "corp", "corporation", "group", "holdings", "sa", "rsa",
}
_PLACEHOLDERS = {"", "-", "--", "n/a", "na", "none", "null", "nan", "tbc", "tbd", "?", "in ›", "in >", "in"}


def clean(value: Any) -> str | None:
    """Trim a cell value; placeholders and blanks become None."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return None if s.lower() in _PLACEHOLDERS else s


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(name: str | None) -> str:
    """Person-name key: accent-free, lowercase, punctuation/hyphens -> single spaces."""
    s = strip_accents(name or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def norm_company(name: str | None) -> str:
    """Company key: drops legal suffixes like (Pty) Ltd, '&' -> 'and'."""
    s = strip_accents(name or "").lower().replace("&", " and ")
    tokens = re.sub(r"[^a-z0-9]+", " ", s).split()
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def slug(text: str | None) -> str:
    """Filename-safe token: 'Leandro da Cunha' -> 'Leandro_da_Cunha', 'Rene Muller' for 'René Müller'."""
    s = strip_accents(text or "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_")


def split_name(full_name: str) -> tuple[str, str]:
    """First token is the first name, the rest is the surname ('Leandro da Cunha' -> Leandro / da Cunha)."""
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def segment_of(value: str | None) -> str:
    s = (value or "").strip().lower()
    if s.startswith("comp"):
        return "competitor"
    if s.startswith("int"):
        return "internal"
    return "customer"


def company_token(company: str | None) -> str:
    """Default competitor filename token: first word of the company, alnum only."""
    words = slug(company).split("_")
    return words[0] if words and words[0] else "COMP"


def segment_token(segment: str, company: str | None, cfg: dict | None = None) -> str:
    seg_cfg = (cfg or {}).get("segments", {}) if cfg else {}
    if segment == "customer":
        return seg_cfg.get("customer_token", "CUST")
    if segment == "internal":
        return seg_cfg.get("internal_token", "INT")
    for acct, token in (seg_cfg.get("competitor_accounts") or {}).items():
        if norm_company(acct) == norm_company(company):
            return token
    return company_token(company)


def face_filename(segment: str, full_name: str, company: str | None, cfg: dict | None = None) -> str:
    """<Segment>__<First>_<Last>.jpg"""
    return f"{segment_token(segment, company, cfg)}__{slug(full_name)}.jpg"


def parse_face_filename(filename: str) -> tuple[str, str] | None:
    """'Duxbury__Leandro_da_Cunha.jpg' -> ('Duxbury', 'Leandro_da_Cunha')."""
    m = re.match(r"^(?P<seg>[^_][^.]*?)__(?P<name>.+)\.(jpe?g|png|webp)$", filename, re.IGNORECASE)
    if not m:
        return None
    return m.group("seg"), m.group("name")


def token_segment(token: str, cfg: dict | None = None) -> str:
    seg_cfg = (cfg or {}).get("segments", {}) if cfg else {}
    t = token.upper()
    if t == seg_cfg.get("customer_token", "CUST").upper():
        return "customer"
    if t == seg_cfg.get("internal_token", "INT").upper():
        return "internal"
    return "competitor"


def parse_money(value: Any) -> float | None:
    """'R 1,234,567.89' / '1.2m' / '(1 200)' / 16341175.1 -> float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    s = str(value).strip().lower()
    neg = s.startswith(("(", "-"))
    s = re.sub(r"[()\-\s ,]", "", s)
    s = re.sub(r"^(zar|r)", "", s)
    mult = 1.0
    if s.endswith("m"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("k"):
        mult, s = 1e3, s[:-1]
    try:
        n = float(s) * mult
    except ValueError:
        return None
    return -n if neg else n


def is_url(value: str | None) -> bool:
    return bool(value) and bool(re.match(r"^https?://", value.strip(), re.IGNORECASE))


def linkedin_url(value: Any) -> str | None:
    s = clean(value)
    if not s:
        return None
    if s.lower().startswith(("linkedin.com", "www.linkedin.com")):
        s = "https://" + s
    return s if is_url(s) and "linkedin.com" in s.lower() else None


def as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
