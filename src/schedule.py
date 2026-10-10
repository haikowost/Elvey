"""What Windows Task Scheduler will run, for the dashboard's KYC panel.

    OLD_TASK  "Elvey LinkedIn harvest" - the retired every-2-hours run_harvest.ps1 job; should not exist
    TASK      "Elvey KYC daily"        - the one opt-in daily run (scripts/schedule_kyc.ps1)

On Windows this asks `schtasks /Query`; elsewhere (or if that fails) it reads data/schedule.json, which
schedule_kyc.ps1 writes and unschedule_kyc.ps1 removes. No task = "manual only".
"""
from __future__ import annotations

import json
import os
import subprocess
import time

OLD_TASK = "Elvey LinkedIn harvest"
TASK = "Elvey KYC daily"
_cache: dict = {"at": 0.0, "value": None}


def _query(name: str) -> dict | None:
    """schtasks /Query /V /FO LIST -> {'next_run': ..., 'status': ..., 'task_to_run': ...}, or None."""
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    info: dict = {}
    for line in out.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip().lower()] = v.strip()
    return {"next_run": info.get("next run time"), "status": info.get("status"),
            "task_to_run": info.get("task to run"), "last_run": info.get("last run time")}


def status(data_dir, use_cache: bool = True) -> dict:
    """{'mode': 'scheduled' | 'manual only', 'next_run', 'task', 'old_task_present'}."""
    if use_cache and _cache["value"] and time.monotonic() - _cache["at"] < 300:
        return _cache["value"]
    value: dict = {"mode": "manual only", "next_run": None, "task": None, "old_task_present": False}
    if os.name == "nt":
        old, new = _query(OLD_TASK), _query(TASK)
        value["old_task_present"] = old is not None
        if new:
            value.update(mode="scheduled", next_run=new["next_run"], task=TASK, status=new["status"])
    else:
        f = data_dir / "schedule.json"
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            value.update(mode="scheduled", next_run=s.get("next_run") or f"daily at {s.get('time')}", task=s.get("task"))
        except (OSError, ValueError):
            pass
    _cache.update(at=time.monotonic(), value=value)
    return value
