@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  Xiaodu x MusicFree service launcher
REM  This only starts the daemon; the daemon keeps the service
REM  and tunnel alive, and syncs the new tunnel URL automatically.
REM ============================================================

set ROOT=%~dp0

REM ---- start daemon (hidden window) ----
start "xiaodu-daemon" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%daemon.ps1"

echo.
echo  Daemon started.
echo  It keeps the service and tunnel alive automatically.
echo.
echo  Check current tunnel URL:
echo    type "%ROOT%logs\current_url.txt"
echo.
echo  Check daemon log:
echo    type "%ROOT%logs\daemon.log"
echo.

endlocal
