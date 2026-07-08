@echo off
REM ── run_rfx.bat ──────────────────────────────────────────────
REM Double-click to run the RFx digest. Keep this file inside your
REM project folder (next to rfx_alert.py); it needs no editing,
REM it switches to its own folder automatically.

cd /d "%~dp0"

echo Running RFx digest...
echo.
py rfx_alert.py

echo.
echo ----------------------------------------------
echo Done. Press any key to close this window.
pause >nul
