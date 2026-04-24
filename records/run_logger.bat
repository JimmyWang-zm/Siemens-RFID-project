
@echo off
setlocal

REM One-click launcher for workers
cd /d "%~dp0"

if not exist logs mkdir logs

where python >nul 2>nul
if %errorlevel%==0 (
    python -u "rfid_logger.py" --mode xml --url "https://192.168.0.254/" --xml-port 10001 --timeout 8 --log-all --csv "logs/rfid_reads.csv" --jsonl "logs/rfid_reads.jsonl"
) else (
    py -3 -u "rfid_logger.py" --mode xml --url "https://192.168.0.254/" --xml-port 10001 --timeout 8 --log-all --csv "logs/rfid_reads.csv" --jsonl "logs/rfid_reads.jsonl"
)

echo.
echo Logger stopped. Press any key to close.
pause >nul
