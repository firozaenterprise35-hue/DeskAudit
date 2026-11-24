; DeskAudit installer — Inno Setup 6 script.
; Build the exe first (build_exe.bat), then compile this with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\DeskAudit.iss
; Output: installer\Output\DeskAudit-Setup-10.0.exe
; Installs per-user to %LOCALAPPDATA%\DeskAudit (no admin needed) so the
; agent can write its data/, reports/ and backups/ folders next to the exe.

#define MyAppName "DeskAudit"
#define MyAppVersion "10.0"
#define MyAppPublisher "Firoza"
#define MyAppExeName "DeskAudit.exe"

[Setup]
AppId={{A3A33CF3-E5E2-43E0-89EF-F91E37C55030}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
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
Name: "startup"; Description: "Start DeskAudit when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\DeskAudit\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\DeskAudit"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\DeskAudit Dashboard"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--dashboard"; WorkingDir: "{app}"
Name: "{group}\Uninstall DeskAudit"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DeskAudit"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DeskAudit"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--once"; Description: "Run a quick smoke test now"; Flags: postinstall skipifsilent unchecked
