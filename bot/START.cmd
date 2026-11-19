@echo off
setlocal
title XAU Scalper
cd /d "%~dp0"

rem --- refuse to run from inside a ZIP preview / temp folder (Windows extracts to Temp silently)
echo "%~dp0" | findstr /i /c:"\AppData\Local\Temp\" >nul
if not errorlevel 1 goto zipwarn

rem --- first run: remove the "downloaded from the internet" mark so Windows doesn't block the bundled Python
if not exist "data\.unblocked" (
    if not exist "data" mkdir "data"
    set "TRAD_DIR=%~dp0"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath $env:TRAD_DIR -Recurse -File | Unblock-File" >nul 2>&1
    echo ok> "data\.unblocked"
)
set PYTHONUTF8=1

if exist "runtime\pythonw.exe" (
    start "" "runtime\pythonw.exe" "%~dp0main.py" ui
    exit /b 0
)

rem --- source checkout: build a local venv with 64-bit Python 3.12 / 3.13
if exist ".venv\Scripts\pythonw.exe" goto deps
for %%V in (3.12 3.13) do (
    py -%%V -c "import struct; assert struct.calcsize('P') == 8" >nul 2>nul
    if not errorlevel 1 (
        py -%%V -m venv .venv
        if not errorlevel 1 goto deps
    )
)
echo Install 64-bit Python 3.12 from python.org, then open START.cmd again.
pause
exit /b 1
:deps
".venv\Scripts\python.exe" -c "import MetaTrader5, pandas, lightgbm, yaml, matplotlib, sklearn, pyarrow" 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -r requirements.txt
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py" ui
exit /b 0

:zipwarn
echo.
echo  This folder is inside a ZIP file or a temporary folder.
echo  Right-click the downloaded ZIP, choose "Extract All...", pick a folder such as C:\TradV2,
echo  then open START.cmd from the extracted folder.
echo.
pause
exit /b 1
