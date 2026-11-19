"""Assemble and verify the downloadable Windows x64 release on a Windows builder."""
from __future__ import annotations

import hashlib
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = ("README.md","RELEASE_NOTES-1.1.0.md","START.cmd","VERSION","settings.json","requirements.txt","goldbot.py","background.py",
         "broker.py","engine.py","feeds.py","journal.py","learning.py","online_learning.py","research.py","strategy.py")
ASSET = "Trad-Windows-x64.zip"


def main():
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3,13):
        raise SystemExit("Build with 64-bit CPython 3.13 on Windows.")
    dist = ROOT/"dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        app = work/"Trad"
        runtime = app/"runtime"
        runtime.mkdir(parents=True)
        for name in FILES:
            shutil.copyfile(ROOT/name,app/name)
        bundled_tests = app/"tests"
        bundled_tests.mkdir()
        for test in (ROOT/"tests").glob("test_*.py"):
            shutil.copyfile(test,bundled_tests/test.name)
        version = platform.python_version()
        url = f"https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
        with urlopen(url,timeout=90) as response, (work/"python.zip").open("wb") as target:
            shutil.copyfileobj(response,target)
        with ZipFile(work/"python.zip") as z:
            z.extractall(runtime)
        (runtime/"python313._pth").write_text("python313.zip\n.\nLib/site-packages\n..\nimport site\n",encoding="ascii")
        subprocess.run([sys.executable,"-m","pip","install","--disable-pip-version-check","--only-binary=:all:",
                        "--no-compile","--target",str(runtime/"Lib"/"site-packages"),"-r",str(app/"requirements.txt")],check=True)
        exe = str(runtime/"python.exe")
        subprocess.run([exe,"-B","-c","import MetaTrader5, pandas, numpy, sklearn, scipy, tzdata"],cwd=app,check=True)
        subprocess.run([exe,"-B","-m","unittest","discover","-s","tests","-q"],cwd=app,check=True)
        subprocess.run([exe,"-B","goldbot.py","smoke","--output",str(work/"replay")],cwd=app,check=True)
        # The release includes only application, validation and dependency files.
        with ZipFile(dist/ASSET,"w",compression=ZIP_DEFLATED,compresslevel=6) as z:
            for path in sorted(app.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    z.write(path,path.relative_to(work))
    with (dist/ASSET).open("rb") as f:
        digest = hashlib.file_digest(f,"sha256").hexdigest()
    (dist/"SHA256SUMS.txt").write_text(f"{digest}  {ASSET}\n",encoding="ascii")
    print(f"{ASSET} ({(dist/ASSET).stat().st_size:,} bytes), SHA-256 {digest}")


if __name__ == "__main__":
    main()
