@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if not exist "logs" mkdir "logs"
if not exist "logs" exit /b 1
set "PIPELINE_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PIPELINE_PYTHON=.venv\Scripts\python.exe"

echo [%date% %time%] Starting pipeline >> "logs\run_daily.log" 2>&1
"%PIPELINE_PYTHON%" -m pipeline run --config config.yaml >> "logs\run_daily.log" 2>&1
set "PIPELINE_EXIT=%errorlevel%"
echo [%date% %time%] Finished, exit code %PIPELINE_EXIT% >> "logs\run_daily.log" 2>&1

if /I not "%~1"=="scheduled" (
    echo Pipeline exit code: %PIPELINE_EXIT%. See output and logs directories.
    pause
)
exit /b %PIPELINE_EXIT%
