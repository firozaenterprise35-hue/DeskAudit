@echo off
REM DeskAudit — build Setup.exe after build_exe.bat.
REM Needs Inno Setup 6 (https://jrsoftware.org/isdl.php) installed.
cd /d "%~dp0"
if not exist "..\dist\DeskAudit\DeskAudit.exe" (
    echo DeskAudit.exe not found. Run build_exe.bat first.
    pause
    exit /b 1
)
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
    pause
    exit /b 1
)
"%ISCC%" DeskAudit.iss
if %errorlevel% neq 0 (
    echo Installer build FAILED.
    pause
    exit /b 1
)
echo.
echo Done. Installer: installer\Output\DeskAudit-Setup-12.0.exe
pause
