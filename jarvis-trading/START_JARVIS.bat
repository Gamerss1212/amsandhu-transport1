@echo off
rem ============================================================================
rem  Jarvis Trading Portal - Windows launcher. PAPER TRADING ONLY.
rem  Double-click this file. It will:
rem    1. find Python 3.10 or newer
rem    2. install the add-ons in requirements.txt (first run only, or when that file changes)
rem    3. start the portal (python server.py) - your browser opens by itself
rem  Extra options are passed through, e.g.:   START_JARVIS.bat --demo
rem ============================================================================
setlocal EnableExtensions
title Jarvis Trading Portal - paper trading only
cd /d "%~dp0"

echo.
echo   ==================================================
echo     JARVIS TRADING PORTAL  -  paper trading only
echo   ==================================================
echo.

rem ---- 1. find Python 3.10+ : try "python" first, then the "py" launcher ----
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :have_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python
goto :no_python

:have_python
echo   Using:
%PY% --version
echo.

rem ---- 2. install add-ons once; the marker is a copy of requirements.txt ----
set "MARK=.requirements_installed.txt"
if not exist "%MARK%" goto :install
fc /b "requirements.txt" "%MARK%" >nul 2>&1
if errorlevel 1 goto :install
goto :run

:install
echo   First run: installing the add-ons Python needs. This takes about a minute...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :pip_failed
copy /y "requirements.txt" "%MARK%" >nul
echo   Add-ons installed.
echo.
goto :run

:pip_failed
echo.
echo   WARNING: installing the add-ons did not finish. Check your internet connection.
echo   Jarvis will try to start anyway. If it fails, open a terminal in this folder and run:
echo       %PY% -m pip install -r requirements.txt
echo.

:run
rem ---- 3. say which brain mode this will be ----
if defined ANTHROPIC_API_KEY goto :key_found
if defined ANTHROPIC_AUTH_TOKEN goto :key_found
echo   AI brain: no ANTHROPIC_API_KEY found, so Jarvis starts in OFFLINE mode.
echo   Offline mode still does status, report, halt, pause, prices and BUY/WAIT answers.
echo   To add a key, see README.md - section "Add your API key".
echo.
goto :start

:key_found
echo   AI brain: API key found.
echo.

:start
echo   Starting... your browser opens by itself. Use Chrome or Edge for voice.
echo   Keep this window open while you use Jarvis. Press Ctrl+C here to stop it.
echo.
%PY% server.py %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :done

echo.
echo   Jarvis stopped with an error - code %RC%. Read the message above.
echo   Common fixes:
echo     - "WinError 10048" or "address already in use": Jarvis or the old HUD is already
echo       running. Close the other black window, or set a new port in config\jarvis_config.json.
echo     - "No module named ..." or "ZoneInfoNotFoundError": run this in a terminal here:
echo           %PY% -m pip install -r requirements.txt
echo     - Anything else: see README.md - section "Troubleshooting".
echo.
pause
endlocal & exit /b %RC%

:no_python
echo   Python 3.10 or newer was not found on this computer.
echo.
echo   How to fix it - about 3 minutes:
echo     1. Open https://www.python.org/downloads/ and click the big "Download Python" button.
echo     2. Run the installer. On the FIRST screen, tick "Add python.exe to PATH".
echo     3. Click "Install Now" and wait until it says "Setup was successful".
echo     4. Close this window and double-click START_JARVIS.bat again.
echo.
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
