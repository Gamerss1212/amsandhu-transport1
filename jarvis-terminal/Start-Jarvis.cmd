@echo off
setlocal
cd /d "%~dp0"
title Jarvis Terminal - keep this window open
where node >nul 2>nul
if errorlevel 1 (
  echo Jarvis needs Node.js 24 or newer. Install it from https://nodejs.org and run this file again.
  pause
  exit /b 1
)
if not exist "node_modules\@anthropic-ai\sdk\package.json" (
  echo First start: installing the Claude connector. This happens once.
  call npm install --omit=dev --no-audit --no-fund
)
if not exist "data\autostart-installed" (
  if not exist "data" mkdir "data"
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\autostart.ps1" -Install && echo installed> "data\autostart-installed"
)
node launch.mjs %*
if errorlevel 1 pause
