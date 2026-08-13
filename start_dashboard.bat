@echo off
setlocal

rem Start the local historical research dashboard.
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

set "CONFIG=configs\xauusd_baseline.toml"
set "WARMUP_START=2026-07-28T00:00:00Z"
set "DISPLAY_START=2026-08-01T00:00:00Z"
set "DISPLAY_END="
set "HOST=127.0.0.1"
set "PORT=8000"

rem Optional arguments: start end port
if not "%~1"=="" set "DISPLAY_START=%~1"
if not "%~2"=="" set "DISPLAY_END=%~2"
if not "%~3"=="" set "PORT=%~3"

set "END_OPTION="
if not "%DISPLAY_END%"=="" set "END_OPTION=--end %DISPLAY_END%"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer and try again.
    pause
    exit /b 1
)

if not exist "%CONFIG%" (
    echo Config file not found: %CONFIG%
    pause
    exit /b 1
)

python -c "import gold_research" >nul 2>&1
if errorlevel 1 (
    echo Installing the local package...
    python -m pip install -e .
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

echo Starting dashboard at http://%HOST%:%PORT%
start "" "http://%HOST%:%PORT%"
echo Close this window to stop the dashboard.
echo.
python -m gold_research.cli dashboard --config "%CONFIG%" --warmup-start "%WARMUP_START%" --start "%DISPLAY_START%" %END_OPTION% --host "%HOST%" --port %PORT%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" echo Dashboard stopped with error code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
