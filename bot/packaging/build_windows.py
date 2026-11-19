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
TOP_FILES = ["START.cmd", "MENU.cmd", "VERSION", "config.yaml", "requirements.txt", ".env.example", "README.md",
             "ARCHITECTURE.md"]


def app_files() -> list[str]:
    """Everything the app needs: all modules, assets, tests (self-test), tools — never data/ or dist/."""
    files = TOP_FILES + sorted(p.name for p in BOT.glob("*.py"))
    for sub, pat in (("assets", "**/*"), ("tests", "*.py"), ("tools", "*.py")):
        files += sorted(p.relative_to(BOT).as_posix() for p in (BOT / sub).glob(pat) if p.is_file())
    return files


APP_FILES = app_files()


PRUNE_DIRS = {"tests", "test", "__pycache__", "include", "sample_data"}
PRUNE_EXT = {".pyi", ".h", ".hpp", ".pxd", ".pyx", ".lib", ".cmake", ".c", ".cpp"}


def prune(site: Path) -> int:
    """Drop what the libraries ship but never use at runtime (their own test suites, C headers, link
    libraries, type stubs). ~3,500 fewer files → much faster unzip and antivirus scan on Windows."""
    n = 0
    for p in sorted(site.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        if p.is_dir() and p.name in PRUNE_DIRS:
            n += sum(1 for _ in p.rglob("*") if _.is_file())
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file() and p.suffix.lower() in PRUNE_EXT:
            p.unlink()
            n += 1
    return n


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
        print(f"pruned {prune(site):,} unused library files")
        if sys.platform == "win32":
            exe = str(rt / "python.exe")
            subprocess.run([exe, "-B", "-c", "import main, MetaTrader5, pandas, lightgbm, yaml, matplotlib, sklearn, pyarrow"],
                           cwd=app, check=True)
            subprocess.run([exe, "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tests",
                            "-p", "test_demo_execution.py"], cwd=app, check=True)
            subprocess.run([exe, "-X", "utf8", "-B", "main.py", "smoke"], cwd=app, check=True)
            shutil.rmtree(app / "data_smoke", ignore_errors=True)
        # Pack the runtime as ONE inner archive: Explorer then copies ~50 files instead of ~5,000, and
        # START.cmd unpacks it once with Windows' built-in tar (seconds instead of an hour).
        with ZipFile(app / "runtime.zip", "w", ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(rt.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, p.relative_to(rt))
        shutil.rmtree(rt)
        zpath = dist / ASSET
        with ZipFile(zpath, "w", ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(app.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    # the inner runtime.zip is already compressed — store it as-is
                    z.write(p, p.relative_to(work), compress_type=0 if p.name == "runtime.zip" else ZIP_DEFLATED)
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  {ASSET}\n")
    print(f"{zpath} {zpath.stat().st_size / 1e6:.1f} MB sha256 {digest}")


if __name__ == "__main__":
    main()
