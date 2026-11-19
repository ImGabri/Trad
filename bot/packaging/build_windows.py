"""Build TradV2-XAUUSD-Scalper-Windows-x64.zip: app + embedded CPython 3.12 + Windows wheels.

Runs on Windows *or* Linux/macOS (wheels are fetched for win_amd64 with --platform), so the release can be
built anywhere. On Windows the bundled runtime is also smoke-tested.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

BOT = Path(__file__).resolve().parents[1]
PY = "3.12.10"
ASSET = "TradV2-XAUUSD-Scalper-Windows-x64.zip"
APP_FILES = ["main.py", "config.py", "config.yaml", "data.py", "features.py", "strategy.py", "risk.py",
             "backtest.py", "ai_model.py", "execution.py", "live.py", "monitor.py", "news.py", "prepare.py",
             "background.py", "START.cmd", "VERSION", "requirements.txt", ".env.example", "README.md",
             "ARCHITECTURE.md", "tests/synthetic.py", "tests/fake_mt5.py", "tests/test_pipeline.py",
             "tests/test_live.py", "tools/probe_symbols.py"]


def main():
    dist = BOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        app = work / "TradV2"
        rt = app / "runtime"
        rt.mkdir(parents=True)
        for f in APP_FILES:
            (app / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BOT / f, app / f)
        url = f"https://www.python.org/ftp/python/{PY}/python-{PY}-embed-amd64.zip"
        print("downloading", url)
        with urlopen(url, timeout=120) as r, (work / "py.zip").open("wb") as out:
            shutil.copyfileobj(r, out)
        with ZipFile(work / "py.zip") as z:
            z.extractall(rt)
        tag = "python" + "".join(PY.split(".")[:2])
        (rt / f"{tag}._pth").write_text(f"{tag}.zip\n.\nLib/site-packages\n..\nimport site\n", encoding="ascii")
        site = rt / "Lib" / "site-packages"
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                        "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.12",
                        "--implementation", "cp", "--target", str(site), "-r", str(app / "requirements.txt"),
                        "MetaTrader5"], check=True)
        if sys.platform == "win32":
            exe = str(rt / "python.exe")
            subprocess.run([exe, "-B", "-c", "import main, MetaTrader5, pandas, lightgbm, yaml, matplotlib"], cwd=app, check=True)
            subprocess.run([exe, "-B", "main.py", "smoke"], cwd=app, check=True)
            shutil.rmtree(app / "data_smoke", ignore_errors=True)
        zpath = dist / ASSET
        with ZipFile(zpath, "w", ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(app.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, p.relative_to(work))
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  {ASSET}\n")
    print(f"{zpath} {zpath.stat().st_size / 1e6:.1f} MB sha256 {digest}")


if __name__ == "__main__":
    main()
