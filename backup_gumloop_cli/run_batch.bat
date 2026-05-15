@echo off
REM Batch Gumloop University Automation - Windows Runner

echo ================================
echo Batch Gumloop Automation (NEW)
echo ================================
echo.

if "%~1"=="" (
    echo Usage:
    echo.
    echo Single account:
    echo   run_batch.bat single EMAIL PASSWORD MCP_URL
    echo.
    echo Batch mode:
    echo   run_batch.bat batch ACCOUNTS_FILE MCP_URL
    echo.
    echo Examples:
    echo   run_batch.bat single user@example.com pass123 http://localhost:1430
    echo   run_batch.bat batch accounts.txt http://localhost:1430
    echo.
    exit /b 1
)

set MODE=%~1

if "%MODE%"=="single" (
    set EMAIL=%~2
    set PASSWORD=%~3
    set MCP_URL=%~4
    
    echo Mode: Single Account
    echo Email: %EMAIL%
    echo.
    
    python batch_gumloop_university.py --email "%EMAIL%" --password "%PASSWORD%" --mcp-url "%MCP_URL%"
    
) else if "%MODE%"=="batch" (
    set BATCH_FILE=%~2
    set MCP_URL=%~3
    
    echo Mode: Batch Processing
    echo File: %BATCH_FILE%
    echo.
    
    python batch_gumloop_university.py --batch "%BATCH_FILE%" --mcp-url "%MCP_URL%"
    
) else (
    echo ❌ Invalid mode: %MODE%
    echo Use "single" or "batch"
    exit /b 1
)

echo.
echo ================================
echo Automation Complete
echo ================================
pause
