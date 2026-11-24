@echo off
REM DeskAudit — schedule the 9pm daily job (backup + alerts + AI summary).
REM Double-click this file. It creates a Windows Task Scheduler task
REM named "DeskAudit Daily Summary" that runs every day at 21:00.
REM To remove it later, double-click unschedule_summary.bat
REM or run: python main.py --unschedule-summary
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
schtasks /create /tn "DeskAudit Daily Summary" /tr "\"%PY%\" \"%~dp0main.py\" --evening-job" /sc daily /st 21:00 /f
if %errorlevel%==0 (
    echo Done. DeskAudit will run backup + alerts + daily summary every day at 21:00.
) else (
    echo Failed to create the task. Try right-clicking this file and choosing "Run as administrator".
)
pause
