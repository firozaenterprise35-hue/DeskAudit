@echo off
REM DeskAudit — schedule the 9pm daily job (backup + alerts + AI summary).
REM Double-click this file. It creates a Windows Task Scheduler task
REM named "DeskAudit Daily Summary" that runs every day at 21:00.
REM To remove it later, double-click unschedule_summary.bat
REM or run: DeskAudit.exe --unschedule-summary
REM (from source: python main.py --unschedule-summary)
cd /d "%~dp0"
if exist "%~dp0DeskAudit.exe" (
    REM Installed / portable exe: use it (it has no console window,
    so this script prints the result itself).
    "%~dp0DeskAudit.exe" --schedule-summary
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0main.py" --schedule-summary
) else (
    python "%~dp0main.py" --schedule-summary
)
if %errorlevel%==0 (
    echo Done. DeskAudit will run backup + alerts + daily summary every day at 21:00.
) else (
    echo Failed to create the task. Try right-clicking this file and choosing "Run as administrator".
)
pause
