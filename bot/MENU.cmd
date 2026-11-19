@echo off
setlocal
title XAU Scalper - advanced menu
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "runtime\python.exe" if exist "runtime.zip" ( echo Run START.cmd once first - it unpacks the bundled Python. & pause & exit /b 1 )
if exist "runtime\python.exe" ( "runtime\python.exe" main.py menu ) else ( ".venv\Scripts\python.exe" main.py menu )
if errorlevel 1 pause
