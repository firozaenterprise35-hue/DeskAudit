@echo off
REM DeskAudit — remove the 9pm daily job created by schedule_summary.bat
cd /d "%~dp0"
if exist "%~dp0DeskAudit.exe" (
    "%~dp0DeskAudit.exe" --unschedule-summary
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0main.py" --unschedule-summary
) else (
    python "%~dp0main.py" --unschedule-summary
)
if %errorlevel%==0 (
    echo Done. The 9pm DeskAudit job has been removed.
) else (
    echo No "DeskAudit Daily Summary" task was found, or it could not be removed.
)
pause
