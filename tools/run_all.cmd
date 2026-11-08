@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

set "PY=C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if not exist "%PY%" (
  echo [FAIL] 找不到解释器：%PY%
  pause
  exit /b 2
)

echo 运行验收：preflight - 红线 - 功能 - 前端
echo.
"%PY%" tools\run_all.py
set "RC=%ERRORLEVEL%"

echo.
echo 退出码：%RC%
if "%RC%"=="2" echo 服务未启动，请先双击 启动.cmd
if "%RC%"=="1" echo 存在 FAIL 项，详见 docs\verify\REPORT.md

echo.
pause
exit /b %RC%
