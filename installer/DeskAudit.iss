; DeskAudit installer — Inno Setup 6 script.
; Build the exe first (build_exe.bat), then compile this with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\DeskAudit.iss
; Output: installer\Output\DeskAudit-Setup-12.14.3.exe
; Installs per-user to %LOCALAPPDATA%\DeskAudit (no admin needed) so the
; agent can write its data/, reports/ and backups/ folders next to the exe.

#define MyAppName "DeskAudit"
#define MyAppVersion "12.14.3"
#define MyAppPublisher "Firoza"
#define MyAppExeName "DeskAudit.exe"

[Setup]
AppId={{A3A33CF3-E5E2-43E0-89EF-F91E37C55030}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
SetupIconFile=deskaudit.ico
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=DeskAudit-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start DeskAudit when Windows starts"; GroupDescription: "Startup:"; Flags: checked

[Files]
; config.local.yaml (Gemini key + Telegram token) is installed with
; uninsneveruninstall (v12.13, M16): the old wildcard installed it as
; an ordinary file, so Inno deleted it on uninstall — wiping the key
; and token. It is excluded from the wildcard below so the two entries
; do not fight. skipifsourcedoesntexist: a clean build without secrets
; still compiles; the key/token are then entered in Dashboard Settings.
Source: "..\dist\DeskAudit\config.local.yaml"; DestDir: "{app}"; Flags: uninsneveruninstall skipifsourcedoesntexist
Source: "..\dist\DeskAudit\*"; Excludes: "config.local.yaml"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\DeskAudit"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--hidden"; WorkingDir: "{app}"
Name: "{group}\DeskAudit Dashboard"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-dashboard"; WorkingDir: "{app}"
Name: "{group}\Schedule 9pm daily job"; Filename: "{app}\schedule_summary.bat"; WorkingDir: "{app}"
Name: "{group}\Stop DeskAudit"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--stop"; WorkingDir: "{app}"
Name: "{group}\Uninstall DeskAudit"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DeskAudit"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--hidden"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DeskAudit"; ValueData: """{app}\{#MyAppExeName}"" --hidden"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--once"; Description: "Run a quick smoke test now"; Flags: postinstall skipifsilent unchecked

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    { Upgrading over a running agent would fail on the locked exe, so
      stop it first (harmless when it is not running). }
    Exec('taskkill.exe', '/F /IM DeskAudit.exe', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Remove the 9pm scheduled task (v12.13, M15): it pointed at the
      deleted exe and fired daily, failing silently after uninstall. }
    Exec('schtasks.exe', '/delete /tn "DeskAudit Daily Summary" /f', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
