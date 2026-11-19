@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="legacy" goto legacy
if not exist "bot\START.cmd" goto legacy
call "%~dp0bot\START.cmd"
exit /b %errorlevel%
:legacy
if exist "runtime\python.exe" (
    "runtime\python.exe" goldbot.py menu
    if errorlevel 1 pause
    exit /b
)
if exist ".venv\Scripts\python.exe" goto deps
for %%V in (3.12 3.13 3.11) do (
    py -%%V -c "import struct; assert struct.calcsize('P') == 8" >nul 2>nul
    if not errorlevel 1 (
        py -%%V -m venv .venv
        if not errorlevel 1 goto deps
    )
)
python -c "import sys,struct; assert (3,11) <= sys.version_info[:2] <= (3,13) and struct.calcsize('P') == 8" >nul 2>nul
if errorlevel 1 goto missing
python -m venv .venv
if not errorlevel 1 goto deps
:missing
echo Install 64-bit Python 3.12 or 3.13 for Windows, then open START.cmd again.
pause
exit /b 1
:deps
".venv\Scripts\python.exe" -c "import MetaTrader5, pandas, numpy, sklearn, tzdata" 2>nul
if not errorlevel 1 goto menu
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    pause
    exit /b 1
)
:menu
".venv\Scripts\python.exe" goldbot.py menu
if errorlevel 1 pause
