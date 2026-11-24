@echo off
REM DeskAudit — USB write protection ON (needs admin).
REM Sets HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies\WriteProtect = 1,
REM so Windows blocks WRITING to USB pen drives / external drives on this PC.
REM Reading from USB still works, and the DeskAudit agent still logs every
REM USB insert / copy attempt. Use usb_write_allow.bat to turn it off again.
REM Note: this does not block phones (MTP) or cloud uploads — those are
REM caught and logged by the Data Transfer Monitor instead.

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
reg add "HKLM\SYSTEM\CurrentControlSet\Control\StorageDevicePolicies" /v WriteProtect /t REG_DWORD /d 1 /f
if %errorlevel% equ 0 (
  echo.
  echo USB write protection is ON. Unplug and re-plug a USB drive for it to take effect.
) else (
  echo.
  echo Could not set the registry value. Make sure you ran this as administrator.
)
pause
