@echo off
rem ===================================================================
rem  start.bat - Windows launcher (double-click friendly).
rem  THIS FILE MUST STAY PURE ASCII. Do not add Chinese here.
rem
rem  Why (CP-D on-site finding): cmd.exe parses the BYTES of a .bat using
rem  the CONSOLE CODE PAGE. Non-ASCII text can be split mid-character and
rem  then show up as:
rem      '...' is not recognized as an internal or external command
rem      " was unexpected at this time.
rem  and when that happens inside an "if ... ( ... )" block, the message we
rem  actually want the operator to see (e.g. "port already in use") is lost.
rem  Therefore: every human-facing Chinese string lives in scripts\launch.py
rem  (Python owns its own encoding); this script is ASCII-only plumbing, so
rem  the console code page can no longer break it.
rem
rem  Line endings must be CRLF (.gitattributes forces *.bat eol=crlf):
rem    cmd.exe mis-parses LF-only .bat files in blocks/goto scenarios.
rem  Requires Python 3.11+; no Docker, no network (ADR-0003).
rem ===================================================================
setlocal
rem Switch the console to UTF-8 so Chinese shown by Python/Flask renders
rem correctly. Nothing below DEPENDS on this succeeding.
chcp 65001 >nul 2>nul
cd /d "%~dp0"

rem Make Python's stdout/stderr UTF-8 even when redirected to a file
rem (otherwise Python falls back to the system locale and Chinese turns
rem into mojibake in captured logs).
set "PYTHONUTF8=1"

set "PY_CMD="
where python >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"
if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    rem This is the ONE message that cannot come from Python (Python is
    rem missing), so it stays ASCII on purpose.
    echo ====================================================================
    echo  [FAILED] Python 3.11+ not found.
    echo  Install Python 3.11 or newer and tick "Add python.exe to PATH",
    echo  then double-click this script again.
    echo ====================================================================
    pause
    exit /b 1
)

%PY_CMD% scripts\launch.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" pause

endlocal & exit /b %EXIT_CODE%
