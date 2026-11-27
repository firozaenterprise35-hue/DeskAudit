; DeskAudit installer — NSIS script (alternative to installer/DeskAudit.iss).
; Build the exe first (build_exe.bat on Windows, or the Wine build),
; then compile this with NSIS (Windows: makensis; Linux: apt install nsis):
;   makensis installer\DeskAudit.nsi
; Output: installer\Output\DeskAudit-Setup-12.13.exe
; Installs per-user to %LOCALAPPDATA%\DeskAudit (no admin needed) so the
; agent can write its data/, reports/ and backups/ folders next to the exe.
; Paths are relative to this script file, so it compiles from any folder.

!define APP_NAME "DeskAudit"
!define APP_VERSION "12.13"
; Numeric quad for the PE version resource (VIProductVersion needs
; exactly X.X.X.X — APP_VERSION alternates between 2 and 3 parts,
; so keep the quad separate and update it with the version).
!define APP_VERSION_NUM "12.13.0.0"
!define APP_PUBLISHER "Firoza"
!define APP_EXE "DeskAudit.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DeskAudit"

!include "MUI2.nsh"
!include "LogicLib.nsh"

Icon "${__FILEDIR__}\deskaudit.ico"
!define MUI_ICON "${__FILEDIR__}\deskaudit.ico"
!define MUI_UNICON "${__FILEDIR__}\deskaudit.ico"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${__FILEDIR__}/Output/DeskAudit-Setup-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\${APP_NAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma

VIProductVersion "${APP_VERSION_NUM}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "FileDescription" "${APP_NAME} installer"
VIAddVersionKey "LegalCopyright" "${APP_PUBLISHER}"
VIAddVersionKey "FileVersion" "${APP_VERSION_NUM}"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_PARAMETERS "--once"
!define MUI_FINISHPAGE_RUN_TEXT "Run a quick smoke test now"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ; Upgrading over a running agent would fail on the locked exe, so stop
  ; it first (harmless when it is not running).
  ExecWait 'taskkill /F /IM DeskAudit.exe'
FunctionEnd

; Stop the running agent and make sure its exe is really free before
; copying over it. The .onInit taskkill alone is not enough: the agent
; may start again after the installer opens (Start Menu click, the
; 9pm job, the Dashboard shortcut), and then the install fails
; mid-way with "Error opening file for writing: ...DeskAudit.exe".
; So the Section calls this right before the file copy: kill, wait,
; and test whether the exe can be deleted. A locked file fails the
; Delete and sets the error flag; a missing file skips the loop.
Function StopAgent
  IfFileExists "$INSTDIR\${APP_EXE}" 0 stop_done
  StrCpy $R0 0
  stop_try:
    IntOp $R0 $R0 + 1
    ExecWait 'taskkill /F /IM DeskAudit.exe'
    Sleep 2000
    ClearErrors
    Delete "$INSTDIR\${APP_EXE}"
    IfErrors 0 stop_done
    IntCmp $R0 3 stop_fail
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "DeskAudit is still running, so its file cannot be replaced.$\n$\nPlease stop it first (Start Menu -> Stop DeskAudit, or Task Manager -> Details -> DeskAudit.exe -> End task), then click Retry." IDRETRY stop_try
    Abort
  stop_fail:
    MessageBox MB_OK|MB_ICONSTOP "DeskAudit could not be stopped.$\n$\nPlease stop it (Task Manager -> Details -> DeskAudit.exe -> End task) and run Setup again."
    Abort
  stop_done:
FunctionEnd

Function un.StopAgent
  IfFileExists "$INSTDIR\${APP_EXE}" 0 un_stop_done
  StrCpy $R0 0
  un_stop_try:
    IntOp $R0 $R0 + 1
    ExecWait 'taskkill /F /IM DeskAudit.exe'
    Sleep 2000
    ClearErrors
    Delete "$INSTDIR\${APP_EXE}"
    IfErrors 0 un_stop_done
    IntCmp $R0 3 un_stop_fail
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "DeskAudit is still running.$\n$\nPlease stop it first (Start Menu -> Stop DeskAudit, or Task Manager -> Details -> DeskAudit.exe -> End task), then click Retry." IDRETRY un_stop_try
    Abort
  un_stop_fail:
    MessageBox MB_OK|MB_ICONSTOP "DeskAudit could not be stopped.$\n$\nPlease stop it (Task Manager -> Details -> DeskAudit.exe -> End task) and run the uninstaller again."
    Abort
  un_stop_done:
FunctionEnd

Section "${APP_NAME} (required)" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  Call StopAgent
  File /r "${__FILEDIR__}/../dist/${APP_NAME}/*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME} ${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--hidden" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME} Dashboard.lnk" "$INSTDIR\${APP_EXE}" "--open-dashboard" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Schedule 9pm daily job.lnk" "$INSTDIR\schedule_summary.bat" "" "$INSTDIR\schedule_summary.bat" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Stop ${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--stop" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\uninstall.exe" 0
SectionEnd

Section "Desktop icon" SecDesktop
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--hidden" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "Start DeskAudit when Windows starts" SecStartup
  ; The exe is built windowed (no console at all), so it never shows a
  ; CMD window — it just runs in the background.
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_NAME}" '"$INSTDIR\${APP_EXE}" --hidden'
SectionEnd

Section "Uninstall"
  Call un.StopAgent
  ; Remove the 9pm scheduled task (v12.13, M15): it pointed at the
  ; deleted exe and fired daily, failing silently after uninstall.
  ExecWait 'schtasks /delete /tn "DeskAudit Daily Summary" /f'
  ; Preserve the audit history: data/ (the whole database), reports/,
  ; backups/ and config.local.yaml (incl. the Gemini key) are the
  ; user's data, not the install — deleting them on uninstall used to
  ; wipe everything with no warning. Only remove what was installed.
  ; v12.13 (B-m4): the exe used to disappear only as a side effect of
  ; un.StopAgent — delete it explicitly.
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\config.yaml"
  Delete "$INSTDIR\projects.yaml"
  Delete "$INSTDIR\schedule_summary.bat"
  Delete "$INSTDIR\unschedule_summary.bat"
  Delete "$INSTDIR\usb_write_protect.bat"
  Delete "$INSTDIR\usb_write_allow.bat"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"  ; only succeeds when the folder is empty (no data left)
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME} Dashboard.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Schedule 9pm daily job.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Stop ${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_NAME}"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
SectionEnd
