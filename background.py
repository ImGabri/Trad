"""User-controlled Windows logon task for the PU Prime demo runner."""
from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from uuid import uuid4
from xml.etree import ElementTree as ET

import broker
import engine
from research import write_json

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def local_state():
    if os.name != "nt":
        raise RuntimeError("Background startup uses Windows Task Scheduler on the PC running MT5.")
    return Path(os.environ["LOCALAPPDATA"]) / "TradScalper"


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def windows_tool(name, *args, check=True):
    exe = Path(os.environ["SystemRoot"]) / "System32" / (name + ".exe")
    result = subprocess.run([str(exe), *map(str,args)], capture_output=True, text=True,
                            errors="replace", timeout=30)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"{name} returned {result.returncode}")
    return result


def user_sid():
    row = next(csv.reader(windows_tool("whoami","/user","/fo","csv","/nh").stdout.splitlines()))
    if len(row) != 2 or not re.fullmatch(r"S-1-[0-9-]+",row[1]):
        raise RuntimeError("Windows did not return the current user's SID.")
    return row[1]


def task_name(sid):
    return "Trad XAUUSD Demo - " + sid


def task_xml(sid, root, pythonw):
    def element(parent, name, value=None, **attrs):
        node = ET.SubElement(parent, name, attrs)
        node.text = value
        return node
    task = ET.Element("Task", {"version":"1.2", "xmlns":NS})
    registration = element(task,"RegistrationInfo")
    element(registration,"Description","Run the selected PU Prime XAUUSD demo in this user's MT5 session.")
    trigger = element(element(task,"Triggers"),"LogonTrigger")
    element(trigger,"Enabled","true")
    element(trigger,"UserId",sid)
    principal = element(element(task,"Principals"),"Principal",id="User")
    element(principal,"UserId",sid)
    element(principal,"LogonType","InteractiveToken")
    element(principal,"RunLevel","LeastPrivilege")
    settings = element(task,"Settings")
    for key,value in {"MultipleInstancesPolicy":"IgnoreNew", "DisallowStartIfOnBatteries":"false",
                      "StopIfGoingOnBatteries":"false", "StartWhenAvailable":"true",
                      "AllowStartOnDemand":"true", "ExecutionTimeLimit":"PT0S"}.items():
        element(settings,key,value)
    restart = element(settings,"RestartOnFailure")
    element(restart,"Interval","PT1M")
    element(restart,"Count","10")
    action = element(element(task,"Actions",Context="User"),"Exec")
    element(action,"Command",str(pythonw))
    element(action,"Arguments",subprocess.list2cmdline([str(root/"goldbot.py"),"worker"]))
    element(action,"WorkingDirectory",str(root))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def model_file(data):
    model = read_json(data / "research" / "model.json")
    if not model:
        raise ValueError("Run option 2 to train on your PU Prime demo first.")
    return model


def state_info(folder):
    config = read_json(folder/"control.json")
    status = read_json(folder/"status.json")
    if not config.get("enabled"):
        return {"enabled":False, "state":"disabled"}
    base = {"enabled":True,"mode":config["mode"],"symbol":config["symbol"],"folder":config["root"]}
    if status.get("generation") != config["generation"]:
        return dict(base,state="waiting for worker startup")
    age = max(0,round(time.time()-status["heartbeat"]))
    return dict(base,state=status["state"] if age <= 45 else "no recent worker heartbeat",
                detail=status["detail"], heartbeat_age_seconds=age)


def stop(folder, name):
    config = read_json(folder/"control.json")
    if config.get("enabled"):
        config["enabled"] = False
        write_json(folder/"control.json",config)
    # Give an in-flight broker request time to journal its response.
    deadline = time.monotonic()+4
    while config and time.monotonic() < deadline:
        status = read_json(folder/"status.json")
        if (status.get("generation") != config.get("generation") or status.get("state") == "stopped"
                or time.time()-status.get("heartbeat",0) > 45):
            break
        time.sleep(.2)
    if windows_tool("schtasks","/Query","/TN",name,check=False).returncode == 0:
        windows_tool("schtasks","/End","/TN",name,check=False)
        windows_tool("schtasks","/Delete","/TN",name,"/F")


def manage(action, root, data, cfg):
    folder = local_state()
    if action == "status":
        return state_info(folder)
    sid = user_sid()
    name = task_name(sid)
    if action == "stop":
        stop(folder,name)
        return state_info(folder)
    if action not in ("watch","demo"):
        raise ValueError("Select watch, demo, status or stop.")
    model = model_file(data)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise ValueError("Open START.cmd with the supplied Windows runtime or a regular 64-bit Python installation.")
    with broker.connection(cfg) as conn:
        engine.validate_session(conn,cfg,model,action=="demo")
        api, identity, symbol, _ = conn
        terminal = Path(api.terminal_info().path) / "terminal64.exe"
        if not terminal.is_file():
            raise RuntimeError("MT5 did not identify an installed terminal64.exe.")
        binding = {"root":str(root),"data":str(data),"mode":action,"symbol":symbol,"server":identity[1],
                   "account_key":broker.account_key(identity),"terminal":str(terminal)}
    old = read_json(folder/"control.json")
    if old.get("enabled") and all(old.get(k)==v for k,v in binding.items()):
        if windows_tool("schtasks","/Query","/TN",name,check=False).returncode == 0:
            windows_tool("schtasks","/Run","/TN",name)
            return state_info(folder)
    if old.get("enabled"):
        stop(folder,name)
    folder.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp)/"task.xml"
        xml.write_bytes(task_xml(sid,root,pythonw))
        windows_tool("schtasks","/Create","/TN",name,"/XML",xml,"/F")
    binding.update(enabled=True,generation=uuid4().hex)
    try:
        write_json(folder/"control.json",binding)
        windows_tool("schtasks","/Run","/TN",name)
    except Exception:
        stop(folder,name)
        raise
    return state_info(folder)


def supervise(root, data, load_settings, folder):
    binding = read_json(folder/"control.json")
    if not binding.get("enabled") or binding.get("root") != str(root) or binding.get("data") != str(data):
        return
    token = binding["generation"]
    logger = logging.getLogger("trad.background")
    handler = RotatingFileHandler(folder/"background.log",maxBytes=1_000_000,backupCount=2,encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    last = 0.
    phase, detail = "", ""

    def report(state, reason="", force=False):
        nonlocal last, phase, detail
        changed = (state,reason) != (phase,detail)
        if changed:
            logger.info("%s %s",state,reason)
        if changed or force or time.monotonic()-last >= 10:
            write_json(folder/"status.json",{"generation":token,"state":state,"detail":reason,"heartbeat":time.time()})
            last = time.monotonic()
        phase, detail = state,reason

    def active():
        control = read_json(folder/"control.json")
        enabled = control.get("enabled") and control.get("generation")==token
        if enabled:
            report(phase,detail)
        return bool(enabled)

    try:
        delay = 2
        report("starting")
        while active():
            try:
                cfg = load_settings()
                artifact = model_file(data)
                with broker.connection(cfg,terminal_path=binding["terminal"]) as conn:
                    _, identity, symbol, _ = conn
                    if (identity[1],broker.account_key(identity),symbol) != (binding["server"],binding["account_key"],binding["symbol"]):
                        raise RuntimeError("Waiting for the originally selected PU Prime demo and gold symbol.")
                    if not active():
                        break
                    orders = binding["mode"]=="demo"
                    engine.validate_session(conn,cfg,artifact,orders)
                    report("running",binding["mode"])
                    delay = 2
                    engine.run(conn,cfg,artifact,data,orders,active=active)
            except (RuntimeError,ValueError,OSError,sqlite3.Error) as exc:
                report("waiting",str(exc))
            until = time.monotonic()+delay
            while active() and time.monotonic() < until:
                time.sleep(min(.5,max(0,until-time.monotonic())))
            delay = min(60,delay*2)
    except Exception:
        logger.exception("Worker stopped unexpectedly")
        raise
    finally:
        if read_json(folder/"control.json").get("generation")==token:
            report("stopped",force=True)
        logger.removeHandler(handler)
        handler.close()
