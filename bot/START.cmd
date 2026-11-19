@echo off
setlocal
title XAUUSD AI Scalper
cd /d "%~dp0"
if exist "runtime\python.exe" (
    "runtime\python.exe" main.py menu
    if errorlevel 1 pause
    exit /b
)
rem Source checkout: build a local venv with 64-bit Python 3.12 / 3.13
if exist ".venv\Scripts\python.exe" goto deps
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
".venv\Scripts\python.exe" -c "import MetaTrader5, pandas, lightgbm, yaml, matplotlib" 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" main.py menu
if errorlevel 1 pause
