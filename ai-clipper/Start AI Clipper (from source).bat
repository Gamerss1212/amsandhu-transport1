@echo off
rem Backup way to run AI Clipper without the .exe (needs Python 3.10+ from python.org, "Add to PATH" ticked).
cd /d "%~dp0"
if not exist .venv (
  echo First run: installing AI Clipper, this takes a few minutes...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist config.yaml copy config.example.yaml config.yaml >nul
start "" http://127.0.0.1:8000
.venv\Scripts\python -m clipper
pause
