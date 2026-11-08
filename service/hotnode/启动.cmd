@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM creator-os 热点采集旁路服务（DailyHotApi 薄壳）
REM 依赖 D:\aa\refs\DailyHotApi 里的采集实现，只在本机 127.0.0.1:6688 上监听。
REM 关掉这个窗口 = 断掉主路径，主服务会自动回退到内置四源。

where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 node，需要 Node 20 以上。
  pause
  exit /b 1
)

if not exist "node_modules" (
  echo 首次运行，正在安装依赖...
  call npm install --no-audit --no-fund || (echo [错误] 依赖安装失败 & pause & exit /b 1)
)

echo 采集服务地址：http://127.0.0.1:6688
echo 自测：curl http://127.0.0.1:6688/hot/weibo
echo 关闭此窗口即停止服务。
echo.

call npm start
pause
