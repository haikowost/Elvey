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


# --------------------------------------------------------------------------- phone numbers

_TWO_DIGIT_CC = {"20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48",
                 "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65", "66", "81",
                 "82", "84", "86", "90", "91", "92", "93", "94", "95", "98"}


def _group(digits: str) -> str:
    """Groups of 3 from the left, with a final group of 4 when 3s would leave a lone digit
    ('3916338' -> '391 6338', '71729638' -> '71 729 638')."""
    n = len(digits)
    if n <= 4:
        return digits
    if n % 3 == 1:  # 7, 10, 13 digits: …3 3 4
        return " ".join([digits[i:i + 3] for i in range(0, n - 4, 3)] + [digits[-4:]])
    first = n % 3 or 3
    return " ".join([digits[:first]] + [digits[i:i + 3] for i in range(first, n, 3)])


def format_phone(value) -> tuple[str | None, bool]:
    """Standardise a phone number to international format.

    South African numbers become '+27 82 659 7188'; other countries '+267 71 729 638'.
    Returns (formatted, ok). ok=False means the input could not be understood and is returned trimmed.
    Only the first number is kept when a cell holds several ('082 … / 011 …').
    """
    s = clean(value)
    if not s:
        return None, True
    s = re.sub(r"\.0$", "", s.replace(" ", " "))          # Excel float artefacts: 647660619.0
    s = re.split(r"\s*(?:/|;|,|\bor\b)\s*", s)[0].strip()      # first of several numbers
    ext = ""
    m = re.search(r"\s*(?:ext\.?|x|extension)\s*(\d{1,5})$", s, re.IGNORECASE)
    if m:
        ext, s = f" ext {m.group(1)}", s[: m.start()]
    intl = s.lstrip().startswith(("+", "00", "(+"))
    s = re.sub(r"\(\s*0\s*\)", "", s)                           # '+27 (0) 82 …'
    digits = re.sub(r"\D", "", s)
    if not digits or set(digits) == {"0"}:
        return None, True
    if intl and digits.startswith("00"):
        digits = digits[2:]
    if not intl:
        if digits.startswith("27") and len(digits) == 11:
            intl = True
        elif digits.startswith("0") and len(digits) == 10:
            digits, intl = "27" + digits[1:], True
        elif len(digits) == 9 and not digits.startswith("0"):  # SA number whose leading 0 Excel dropped
            digits, intl = "27" + digits, True
        else:
            return s.strip(), False
    if digits.startswith("27"):
        national = digits[2:].lstrip("0")
        if len(national) != 9:
            return "+27 " + national + ext, False
        return f"+27 {national[:2]} {national[2:5]} {national[5:]}{ext}", True
    cc_len = 1 if digits[0] in "17" else 2 if digits[:2] in _TWO_DIGIT_CC else 3
    cc, rest = digits[:cc_len], digits[cc_len:].lstrip("0") if digits[:cc_len] != "39" else digits[cc_len:]
    if len(rest) < 6:
        return "+" + digits + ext, False
    return f"+{cc} {_group(rest)}{ext}", True


# --------------------------------------------------------------------------- departments

# which department source may overwrite which (manual edits always win)
DEPT_RANK = {None: 0, "email": 1, "role": 2, "linkedin": 3, "manual": 4}
LOC_RANK = {None: 0, "linkedin": 1, "database": 2, "manual": 3}

DEPARTMENTS = ("Management", "Sales", "Technical", "Projects", "Procurement", "Finance", "Marketing",
               "Operations", "IT", "HR", "Admin")

# Ordered: the first matching function wins, generic management titles last
# ("Sales Director" -> Sales, "Managing Director" -> Management).
_DEPT_RULES = [
    ("Finance", r"financ|accountant|accounts (payable|receivable|department|clerk|manager)|creditors|debtors|"
                r"bookkeep|\bcfo\b|treasur|billing|credit control|payroll|auditor|controller"),
    ("Procurement", r"procure|buyer|buying|purchas|supply chain|sourcing|\bstores?\b|vendor manag"),
    ("Sales", r"\bsales\b|pre-?sales|account manager|account executive|key account|business development|\bbdm\b|"
              r"\bbd\b|channel manager|partner manager|agency manager|commercial manager|tender|estimator|"
              r"quotations?|\bsdr\b|customer success"),
    ("Marketing", r"marketing|\bbrand\b|communications|\bpr\b|digital|social media|content"),
    ("HR", r"\bhr\b|human resources|people (and|&) culture|talent|recruit"),
    ("IT", r"\bit\b|\bict\b|information technology|systems? admin|sysadmin|developer|software|\bcio\b|\bcto\b|"
           r"devops|database|cyber"),
    ("Projects", r"project|site manager|contracts manager|\bpmo\b"),
    ("Technical", r"engineer|technician|technical|installer|integrat|solutions? architect|design|cctv|"
                  r"security systems|commissioning|network|electrical|maintenance|\bfire\b|access control"),
    ("Operations", r"operations|logistics|warehouse|dispatch|fleet|production|service delivery|control room|"
                   r"security manager|risk"),
    ("Admin", r"admin|reception|assistant|secretary|office manager|\bpa\b|coordinator|customer service|"
              r"help ?desk|support"),
    ("Management", r"\bceo\b|\bmd\b|managing director|director|owner|founder|general manager|\bgm\b|president|"
                   r"partner|\bcoo\b|chairman|chief|head of|executive|principal|branch (cluster )?manager|"
                   r"country manager|regional manager|\bmanager\b"),
]
_DEPT_COMPILED = [(d, re.compile(p, re.IGNORECASE)) for d, p in _DEPT_RULES]
_EMAIL_DEPT = {"accounts": "Finance", "finance": "Finance", "creditors": "Finance", "debtors": "Finance",
               "procurement": "Procurement", "purchasing": "Procurement", "buyer": "Procurement",
               "sales": "Sales", "marketing": "Marketing", "hr": "HR", "admin": "Admin", "reception": "Admin",
               "it": "IT", "support": "Technical", "technical": "Technical", "projects": "Projects"}


def classify_department(*titles: str | None, email: str | None = None) -> str | None:
    """Best-guess department from a job title / LinkedIn headline, else from a role mailbox (accounts@…)."""
    for title in titles:
        t = (title or "").strip()
        if not t:
            continue
        t = t.split("|")[0].split(" at ")[0]  # 'Senior Buyer at Acme | …' -> 'Senior Buyer'
        for dept, rx in _DEPT_COMPILED:
            if rx.search(t):
                return dept
    local = (email or "").split("@")[0].lower()
    return _EMAIL_DEPT.get(re.sub(r"[^a-z]", "", local))


# --------------------------------------------------------------------------- companies

def same_company(a: str | None, b: str | None) -> bool:
    """Loose company match: 'Fidelity-ADT' ~ 'Fidelity ADT (Pty) Ltd' ~ 'Fidelity Services Group'."""
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = na.split(), nb.split()
    if ta[0] == tb[0] and len(ta[0]) >= 4:
        return True
    try:
        from rapidfuzz import fuzz

        return fuzz.token_set_ratio(na, nb) >= 88
    except ImportError:  # pragma: no cover
        return False


# --------------------------------------------------------------------------- location

def split_location(raw: Any) -> tuple[str | None, str | None, str | None]:
    """'Johannesburg, Gauteng, South Africa' -> (city, province, country).

    Two parts (no clear province, e.g. 'Gaborone, Botswana') -> (city, None, country).
    One part -> (None, None, country). Terminology note: this is stored as province (the term
    used in South Africa) but maps to Zoho's standard Contacts.Mailing_State field.
    """
    s = clean(raw)
    if not s:
        return None, None, None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return None, None, parts[0]
