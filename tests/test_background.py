import json
import os
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from xml.etree import ElementTree as ET

import background
import broker
from research import write_json


class Background(unittest.TestCase):
    def test_xml_uses_own_session_and_windowless_runtime(self):
        root = PureWindowsPath("C:/Trading & Research/Trad")
        exe = root/"runtime"/"pythonw.exe"
        sid = "S-1-5-21-123456789-1001"
        xml = ET.fromstring(background.task_xml(sid,root,exe))
        def field(path):
            return xml.findtext(path,namespaces={"":background.NS})
        self.assertEqual(field("Triggers/LogonTrigger/UserId"),sid)
        self.assertEqual(field("Principals/Principal/LogonType"),"InteractiveToken")
        self.assertEqual(field("Principals/Principal/RunLevel"),"LeastPrivilege")
        self.assertEqual(field("Actions/Exec/Command"),str(exe))
        self.assertEqual(field("Actions/Exec/Arguments"),f'"{root / "goldbot.py"}" worker')
        self.assertEqual(field("Actions/Exec/WorkingDirectory"),str(root))
        self.assertEqual(field("Settings/ExecutionTimeLimit"),"PT0S")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root/"data"
        self.control = self.root/"session"
        self.control.mkdir()
        self.identity = (1234,"PUPrime-Demo")
        self.binding = {"enabled":True,"generation":"one","root":str(self.root),"data":str(self.data),
                        "mode":"watch","symbol":"XAUUSD.s","server":self.identity[1],
                        "account_key":broker.account_key(self.identity),"terminal":str(self.root/"terminal64.exe")}
        write_json(self.control/"control.json",self.binding)
        write_json(self.data/"research"/"model.json",{"source":{"kind":"fixture"}})
    def tearDown(self):
        self.temp.cleanup()
    def stop_worker(self):
        write_json(self.control/"control.json",{**self.binding,"enabled":False})
    def test_disabled_worker_never_opens_mt5(self):
        self.stop_worker()
        with patch("broker.connection") as connect:
            background.supervise(self.root,self.data,dict,self.control)
        connect.assert_not_called()
    def test_restart_rejects_different_demo_on_same_server(self):
        @contextmanager
        def other(*args,**kwargs):
            yield object(),(999,self.identity[1]),"XAUUSD.s",{}
        with patch("broker.connection",other), patch("engine.run") as run, \
             patch("background.time.sleep",side_effect=lambda _:self.stop_worker()):
            background.supervise(self.root,self.data,dict,self.control)
        run.assert_not_called()
        self.assertIn("originally selected",(self.control/"background.log").read_text())
    def test_reconnects_and_resumes_selected_mode(self):
        attempts = []
        @contextmanager
        def reconnect(cfg,terminal_path):
            attempts.append(terminal_path)
            if len(attempts)==1:
                raise RuntimeError("terminal offline")
            yield object(),self.identity,"XAUUSD.s",{}
        def run(conn,cfg,artifact,folder,orders,active):
            self.assertFalse(orders)
            self.assertTrue(active())
            self.stop_worker()
            self.assertFalse(active())
        with patch("broker.connection",reconnect), patch("engine.validate_session"), patch("engine.run",side_effect=run) as start, \
             patch("background.time.monotonic",side_effect=iter(range(0,200,5)).__next__):
            background.supervise(self.root,self.data,dict,self.control)
        self.assertEqual(attempts,[self.binding["terminal"]]*2)
        self.assertEqual(start.call_count,1)
        self.assertEqual(background.state_info(self.control)["state"],"disabled")
    def test_stale_status_is_visible(self):
        write_json(self.control/"status.json",{"generation":"one","state":"running","detail":"watch","heartbeat":time.time()-100})
        self.assertEqual(background.state_info(self.control)["state"],"no recent worker heartbeat")

    def test_install_then_remove_for_selected_account(self):
        python = self.root/"python.exe"
        (self.root/"pythonw.exe").touch()
        (self.root/"terminal64.exe").touch()
        self.stop_worker()
        commands = []
        def os_tool(name,*args,check=True):
            commands.append(args[0])
            if args[0]=="/Run":
                bound=background.read_json(self.control/"control.json")
                self.assertEqual(bound["account_key"],broker.account_key(self.identity))
                self.assertEqual(bound["mode"],"demo")
            return SimpleNamespace(returncode=0)
        @contextmanager
        def connect(cfg):
            yield SimpleNamespace(terminal_info=lambda:SimpleNamespace(path=str(self.root))),self.identity,"XAUUSD.s",{}
        with patch("background.local_state",return_value=self.control),patch("background.user_sid",return_value="S-1-5-99"), \
             patch("background.windows_tool",side_effect=os_tool),patch("sys.executable",str(python)), \
             patch("broker.connection",connect),patch("engine.validate_session"):
            installed=background.manage("demo",self.root,self.data,{})
            self.assertTrue(installed["enabled"])
            self.assertFalse(background.manage("stop",self.root,self.data,{})["enabled"])
        self.assertEqual(commands,["/Create","/Run","/Query","/End","/Delete"])

    @unittest.skipUnless(sys.platform=="win32" and os.environ.get("TRAD_TEST_SCHEDULER")=="1","Windows release-host task test")
    def test_real_scheduler_starts_windowless_worker(self):
        # Register a uniquely named test task and always remove it, including on failure.
        sid = background.user_sid()
        name = "Trad build test "+uuid4().hex
        worker = self.root/"goldbot.py"
        worker.write_text("from pathlib import Path\nimport sys\nPath(__file__).with_name('ready').write_text(sys.argv[1])\n")
        xml = self.root/"task.xml"
        xml.write_bytes(background.task_xml(sid,self.root,Path(sys.executable).with_name("pythonw.exe")))
        background.windows_tool("schtasks","/Create","/TN",name,"/XML",xml,"/F")
        try:
            background.windows_tool("schtasks","/Query","/TN",name,"/XML")
            # GitHub may host its agent in Session 0. InteractiveToken starts only with a signed-in desktop.
            import ctypes
            session = ctypes.c_ulong()
            if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(),ctypes.byref(session)):
                raise ctypes.WinError()
            if session.value == 0:
                self.skipTest("Task registered. Windows host uses service Session 0.")
            background.windows_tool("schtasks","/Run","/TN",name)
            for _ in range(40):
                if (self.root/"ready").is_file():
                    break
                time.sleep(.5)
            self.assertEqual((self.root/"ready").read_text(),"worker")
        finally:
            background.windows_tool("schtasks","/Delete","/TN",name,"/F")
