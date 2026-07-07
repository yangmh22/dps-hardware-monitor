@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo 检查本机监视 daemon_writer 与网页面板 :8080 ...
echo 默认: 若未运行会自动在新窗口启动；仅检查请加 -CheckOnly
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_hwmon_services.ps1" %*
set EC=%ERRORLEVEL%
echo.
echo 退出码 %EC% （0=全部正常）
exit /b %EC%
