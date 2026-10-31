@echo off
title TRADING WITH CLOUDI - ALL IN ONE
color 0B

cd /d "%~dp0"

echo ========================================================
echo       STARTING ALL SERVICES IN ONE SINGLE WINDOW
echo ========================================================
echo.

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment (.venv) not found!
    pause
    exit /b
)

call .venv\Scripts\activate.bat
python run_unified.py %*

pause
