@echo off
REM DeskAudit — build DeskAudit.exe on Windows (PyInstaller, onedir).
REM Needs Python 3.11+ on PATH. Run once; output is dist\DeskAudit\.
REM For a Setup.exe installer, run installer\build_installer.bat after this.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -m venv .venv
)
call .venv\Scripts\activate
pip install -q -r requirements.txt
pip install -q pyinstaller
echo Building DeskAudit.exe ...
pyinstaller DeskAudit.spec --clean --noconfirm
if %errorlevel% neq 0 (
    echo Build FAILED. See the output above.
    pause
    exit /b 1
)
echo.
echo Done. Run it with: dist\DeskAudit\DeskAudit.exe --once
echo (smoke test) or dist\DeskAudit\DeskAudit.exe for the agent.
pause
