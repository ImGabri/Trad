"""Windows background runner: a per-user Task Scheduler logon task (no time limit, restart on failure)
that runs `pythonw main.py worker`. The worker follows data/control.json, so stopping is instant."""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _tool(name, *args, check=True):
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / (name + ".exe")
    r = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, errors="replace", timeout=30)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{name} failed ({r.returncode})")
    return r


def _sid() -> str:
    row = next(csv.reader(_tool("whoami", "/user", "/fo", "csv", "/nh").stdout.splitlines()))
    if len(row) != 2 or not re.fullmatch(r"S-1-[0-9-]+", row[1]):
        raise RuntimeError("Could not read the Windows user SID.")
    return row[1]


def task_name() -> str:
    return "TradV2 XAUUSD Scalper - " + _sid()


def _xml(sid: str, root: Path, pythonw: Path) -> bytes:
    def el(parent, name, value=None, **attrs):
        n = ET.SubElement(parent, name, attrs)
        n.text = value
        return n
    task = ET.Element("Task", {"version": "1.2", "xmlns": NS})
    el(el(task, "RegistrationInfo"), "Description", "XAUUSD AI scalper (PUPrime MT5) — runs at logon.")
    trig = el(el(task, "Triggers"), "LogonTrigger")
    el(trig, "Enabled", "true"); el(trig, "UserId", sid)
    pr = el(el(task, "Principals"), "Principal", id="User")
    el(pr, "UserId", sid); el(pr, "LogonType", "InteractiveToken"); el(pr, "RunLevel", "LeastPrivilege")
    st = el(task, "Settings")
    for k, v in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                 "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true", "AllowStartOnDemand": "true",
                 "ExecutionTimeLimit": "PT0S"}.items():
        el(st, k, v)
    rs = el(st, "RestartOnFailure"); el(rs, "Interval", "PT1M"); el(rs, "Count", "999")
    act = el(el(task, "Actions", Context="User"), "Exec")
    el(act, "Command", str(pythonw))
    el(act, "Arguments", subprocess.list2cmdline([str(root / "main.py"), "worker"]))
    el(act, "WorkingDirectory", str(root))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def control_path(root: Path) -> Path:
    return root / "data" / "control.json"


def read_control(root: Path) -> dict:
    try:
        return json.loads(control_path(root).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def start(root: Path, mode: str):
    if os.name != "nt":
        raise RuntimeError("The background runner uses Windows Task Scheduler.")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("pythonw.exe not found next to python.exe.")
    stop(root, quiet=False)   # let a running worker finish its step and save its state before the task is replaced
    control_path(root).parent.mkdir(exist_ok=True)
    control = {"enabled": True, "mode": mode, "generation": uuid4().hex}
    name = task_name()
    with tempfile.TemporaryDirectory() as tmp:
        x = Path(tmp) / "task.xml"
        x.write_bytes(_xml(_sid(), root, pythonw))
        _tool("schtasks", "/Create", "/TN", name, "/XML", x, "/F")
    control_path(root).write_text(json.dumps(control))
    try:
        _tool("schtasks", "/Run", "/TN", name)
    except Exception:
        control["enabled"] = False
        control_path(root).write_text(json.dumps(control))
        raise


def stop(root: Path, quiet=False):
    c = read_control(root)
    if c:
        c["enabled"] = False
        control_path(root).write_text(json.dumps(c))
    if os.name == "nt":
        name = task_name()
        deadline = time.monotonic() + 6  # let the worker finish its current step
        while time.monotonic() < deadline and not quiet:
            st = _status(root)
            if st.get("state") in ("stopped", "killed", None) or st.get("heartbeat_age_s", 99) > 30:
                break
            time.sleep(0.3)
        if _tool("schtasks", "/Query", "/TN", name, check=False).returncode == 0:
            _tool("schtasks", "/End", "/TN", name, check=False)
            _tool("schtasks", "/Delete", "/TN", name, "/F", check=False)


def _status(root: Path) -> dict:
    from monitor import Status
    return Status.read(root / "data" / "status.json")


def status(root: Path) -> dict:
    c = read_control(root)
    s = _status(root)
    if not c.get("enabled"):
        return {"background": "off", **({"last_state": s.get("state")} if s else {})}
    alive = s.get("heartbeat_age_s", 1e9) < 60
    return {"background": "on", "mode": c.get("mode"), "worker": s.get("state") if alive else "no heartbeat",
            "detail": s.get("detail", ""), "heartbeat_age_s": s.get("heartbeat_age_s"), "version": s.get("version")}
