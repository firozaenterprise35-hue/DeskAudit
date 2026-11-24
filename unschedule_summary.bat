@echo off
REM DeskAudit — remove the 9pm daily job created by schedule_summary.bat
schtasks /delete /tn "DeskAudit Daily Summary" /f
if %errorlevel%==0 (
    echo Done. The 9pm DeskAudit job has been removed.
) else (
    echo No "DeskAudit Daily Summary" task was found, or it could not be removed.
)
pause
