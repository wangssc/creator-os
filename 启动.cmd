@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM Creator OS 启动脚本
REM 本机 PATH 上的 python 是 D:\python.exe（3.14.2，没装 pip 和本项目的依赖），
REM 所以这里显式优先使用能跑起来的解释器。

set "PY="

if exist "C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
  set "PY=C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

if not defined PY (
  for %%P in (python.exe py.exe) do (
    if not defined PY (
      where %%P >nul 2>nul && set "PY=%%P"
    )
  )
)

if not defined PY (
  echo [错误] 找不到 python 解释器。
  pause
  exit /b 1
)

"%PY%" -c "import fastapi, uvicorn, httpx, dotenv" >nul 2>nul
if errorlevel 1 (
  echo [错误] 这个解释器缺少依赖：%PY%
  echo 请先执行：
  echo   "%PY%" -m pip install fastapi uvicorn httpx python-dotenv
  pause
  exit /b 1
)

echo 使用解释器：%PY%
echo 服务地址：http://127.0.0.1:8787
echo 关闭此窗口即停止服务。
echo.

start "" http://127.0.0.1:8787
"%PY%" -m uvicorn app:app --host 127.0.0.1 --port 8787

pause
