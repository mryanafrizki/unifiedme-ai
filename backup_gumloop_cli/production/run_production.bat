@echo off
REM Gumloop University Automation - Production Runner (Windows)

echo ================================
echo Gumloop University Automation
echo Production Mode (No Logs)
echo ================================
echo.

if "%~1"=="" (
    echo Usage: run_production.bat EMAIL PASSWORD MCP_URL [ANSWERS]
    echo.
    echo Examples:
    echo   run_production.bat user@example.com secret123 http://localhost:1430
    echo   run_production.bat user@example.com secret123 http://localhost:1430 "2,3,3,2,2,2,2,2,2,2,2,2,2"
    echo.
    exit /b 1
)

set EMAIL=%~1
set PASSWORD=%~2
set MCP_URL=%~3
set ANSWERS=%~4

echo Email: %EMAIL%
echo MCP URL: %MCP_URL%
echo.

if "%ANSWERS%"=="" (
    python gumloop_university.py --email "%EMAIL%" --password "%PASSWORD%" --mcp-url "%MCP_URL%"
) else (
    python gumloop_university.py --email "%EMAIL%" --password "%PASSWORD%" --mcp-url "%MCP_URL%" --answers "%ANSWERS%"
)

echo.
echo ================================
echo Automation Complete
echo Check result.json for output
echo ================================
pause
