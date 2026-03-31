@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  start_server.bat
::  启动 python main.py --serve-only
::  若指定端口已被占用，则终止占用进程后重新启动
:: ============================================================

:: 读取 .env 中的 WEBUI_PORT，默认 8000
set "PORT=8000"
if exist "%~dp0.env" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /i "^WEBUI_PORT=" "%~dp0.env"`) do (
        set "PORT=%%B"
    )
)
echo [INFO] 使用端口: %PORT%

:CHECK_PORT
echo [INFO] 检查端口 %PORT% 占用情况...
set "PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r ":%PORT%.*LISTENING"') do (
    set "PID=%%P"
)

if defined PID (
    echo [WARN] 端口 %PORT% 被进程 PID=%PID% 占用，正在终止...
    taskkill /PID %PID% /F >nul 2>&1
    if !errorlevel! == 0 (
        echo [INFO] 进程 %PID% 已终止
    ) else (
        echo [ERROR] 无法终止进程 %PID%，请以管理员身份运行
        pause
        exit /b 1
    )
    :: 等待端口释放
    timeout /t 2 /nobreak >nul
    goto CHECK_PORT
)

echo [INFO] 端口 %PORT% 空闲，启动服务...
cd /d "%~dp0"
python main.py --serve-only
if %errorlevel% neq 0 (
    echo [ERROR] 服务异常退出，退出码: %errorlevel%
    pause
)
