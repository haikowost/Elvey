"""Configuration loading: config.yaml + .env (env vars win for paths)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

_ENV_PATHS = {"KYC_FOLDER": ("paths", "kyc_folder"), "KYC_WORKBOOK": ("inputs", "workbook"),
              "KYC_DB": ("paths", "db")}


class Config(dict):
    """Dict with helpers for resolved paths."""

    base: Path

    def path(self, section: str, key: str) -> Path | None:
        value = (self.get(section) or {}).get(key)
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() or _looks_windows_abs(value) else self.base / p

    @property
    def db_path(self) -> Path:
        return self.path("paths", "db")

    @property
    def kyc_folder(self) -> Path:
        return self.path("paths", "kyc_folder")

    @property
    def data_dir(self) -> Path:
        d = self.path("paths", "data_dir") or self.base / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _looks_windows_abs(value: str) -> bool:
    return len(value) > 2 and value[1] == ":" and value[2] in "\\/"


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    path = Path(path or os.environ.get("KYC_CONFIG") or ROOT / "config.yaml")
    with open(path, encoding="utf-8") as fh:
        cfg = Config(yaml.safe_load(fh) or {})
    cfg.base = path.resolve().parent
    for env, (section, key) in _ENV_PATHS.items():
        if os.environ.get(env):
            cfg.setdefault(section, {})[key] = os.environ[env]
    for dotted, value in (overrides or {}).items():
        section, key = dotted.split(".", 1)
        cfg.setdefault(section, {})[key] = value
    return cfg
