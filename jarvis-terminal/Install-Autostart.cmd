@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\autostart.ps1" -Install
if not exist "data" mkdir "data"
echo installed> "data\autostart-installed"
pause
