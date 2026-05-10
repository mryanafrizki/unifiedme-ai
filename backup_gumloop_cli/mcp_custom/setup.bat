@echo off
echo Setting up Gumloop University Automation...
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed!
    exit /b 1
)

echo Python found: 
python --version

REM Create virtual environment
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate venv
echo Activating virtual environment...
call venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt

REM Install Playwright browsers
echo Installing Playwright Chromium...
playwright install chromium

REM Create config from example
if not exist "config.json" (
    echo Creating config.json from example...
    copy config_example.json config.json
    echo.
    echo IMPORTANT: Edit config.json with your credentials before running!
)

REM Create screenshots directory
if not exist "screenshots" mkdir screenshots

echo.
echo Setup complete!
echo.
echo Next steps:
echo 1. Edit config.json with your Gumloop credentials
echo 2. Add your 6 quiz answers to config.json
echo 3. Run: venv\Scripts\activate.bat ^&^& python run_automation.py
echo.
pause
