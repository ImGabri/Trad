"""Publish the tested ZIP and checksum together, and allow an interrupted draft to resume."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from build_windows import ASSET, ROOT

NOTES = ("Download **Trad-Windows-x64.zip**, extract the folder and open **START.cmd**. "
         "The Windows x64 Python runtime and trading libraries are included. "
         "Run option 2 to train on your PU Prime MT5 demo. Menu 8 starts, checks or stops the background runner. "
         "Keep the Windows host awake and signed in with your demo terminal available.")


def gh(*args):
    return subprocess.check_output(["gh",*map(str,args)],text=True).strip()


def publish():
    repo, revision = os.environ["REPOSITORY"], os.environ["REVISION"]
    version = (ROOT/"VERSION").read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+",version):
        raise ValueError("Invalid release version.")
    tag = "v"+version
    releases = json.loads(gh("release","list","--repo",repo,"--limit","500","--json","tagName,isDraft"))
    current = next((r for r in releases if r["tagName"]==tag),None)
    if current is None:
        gh("release","create",tag,"--repo",repo,"--target",revision,"--draft","--title",
           f"Trad {version} for Windows","--notes",NOTES)
    elif current["isDraft"]:
        target = gh("release","view",tag,"--repo",repo,"--json","targetCommitish","--jq",".targetCommitish")
        if target != revision:
            raise RuntimeError("An existing draft belongs to a different revision.")
    elif gh("api",f"repos/{repo}/commits/{tag}","--jq",".sha") != revision:
        raise RuntimeError("The published tag belongs to a different commit.")
    draft = current is None or current["isDraft"]
    if draft:
        gh("release","upload",tag,ROOT/"dist"/ASSET,ROOT/"dist"/"SHA256SUMS.txt","--repo",repo,"--clobber")
    with tempfile.TemporaryDirectory() as temp:
        gh("release","download",tag,"--repo",repo,"--dir",temp,"--pattern",ASSET,"--pattern","SHA256SUMS.txt")
        with (Path(temp)/ASSET).open("rb") as f:
            digest = hashlib.file_digest(f,"sha256").hexdigest()
        if (Path(temp)/"SHA256SUMS.txt").read_text().split() != [digest,ASSET]:
            raise RuntimeError("Downloaded release ZIP and checksum differ.")
    if draft:
        gh("release","edit",tag,"--repo",repo,"--draft=false","--latest")
    print(gh("release","view",tag,"--repo",repo,"--json","url","--jq",".url"))


if __name__=="__main__":
    publish()
