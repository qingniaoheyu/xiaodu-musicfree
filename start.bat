@echo off
chcp 65001 >nul
setlocal

set ROOT=%~dp0
set CF=C:\Program Files (x86)\cloudflared\cloudflared.exe
set LOG=%ROOT%mock\cf.err

if not exist "%ROOT%mock" mkdir "%ROOT%mock"

echo ============================================
echo   小度 x MusicFree 技能服务
echo ============================================
echo.

echo [1/2] 启动技能服务（含隧道自动跟踪）...
start "xiaodu-skill" /min python "%ROOT%bridge\server.py" --config "%ROOT%bridge\config.json" --watch-tunnel
timeout /t 3 >nul

echo [2/2] 启动 Cloudflare Tunnel...
echo   隧道地址会自动写入服务，无需手动改配置。
echo.
echo   拿到 https://xxx.trycloudflare.com 后，到 DuerOS 技能控制台填：
echo       地址 + /dueros
echo.
echo   关闭本窗口即停止隧道；技能服务在最小化窗口里，也要一并关掉。
echo ============================================
echo.

"%CF%" tunnel --url http://127.0.0.1:8765 --no-autoupdate 2>"%LOG%"

endlocal
