@echo off
REM DeskAudit — USB write protection OFF (needs admin).
REM Sets HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies\WriteProtect = 0,
REM so USB pen drives / external drives can be written to again on this PC.
REM The DeskAudit agent keeps logging USB inserts and copies either way.

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
reg add "HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies" /v WriteProtect /t REG_DWORD /d 0 /f
if %errorlevel% equ 0 (
  echo.
  echo USB write protection is OFF. Unplug and re-plug a USB drive for it to take effect.
) else (
  echo.
  echo Could not set the registry value. Make sure you ran this as administrator.
)
pause
