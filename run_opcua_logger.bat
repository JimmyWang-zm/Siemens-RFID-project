@echo off
setlocal

REM One-click launcher for rfid_opcua RFID logger
REM Installs asyncua automatically on first run.

cd /d "%~dp0"

python -c "import asyncua" >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing asyncua...
    pip install asyncua
    if %errorlevel% neq 0 (
        echo Failed to install asyncua. Run manually: pip install asyncua
        pause
        exit /b 1
    )
)

python -u "rfid_opcua_logger.py"

echo.
echo Logger stopped. Press any key to close.
pause >nul
