@echo off
setlocal
chcp 65001 >nul

REM ─────────────────────────────────────────────────────────
REM  RFID OPC UA 自动过站记录程序 — 一键启动
REM  功能 1：Presence 变量检测小车到来/离开，自动 ScanStart/ScanStop
REM  功能 2：将标签写入每日 CSV（位于 C:\rfid_logger\records\）
REM
REM  首次使用请先安装依赖（只需一次）：
REM    pip install asyncua
REM ─────────────────────────────────────────────────────────

cd /d "%~dp0"

REM 检查 asyncua 是否已安装
python -c "import asyncua" >nul 2>nul
if %errorlevel% neq 0 (
    echo [SETUP] 正在安装依赖 asyncua，请稍候...
    pip install asyncua
    if %errorlevel% neq 0 (
        echo [ERR] 安装失败，请手动运行: pip install asyncua
        pause
        exit /b 1
    )
    echo [SETUP] 安装完成。
    echo.
)

echo ============================================================
echo   RFID OPC UA 自动过站记录程序
echo   读写器: opc.tcp://192.168.0.254:4840
echo   记录位置: C:\rfid_logger\records\
echo   Ctrl+C 退出
echo ============================================================
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python -u "rfid_opcua_logger.py"
) else (
    py -3 -u "rfid_opcua_logger.py"
)

echo.
echo 程序已停止。按任意键关闭窗口...
pause >nul
