; Inno Setup script: a per-user installer (no administrator rights needed).
; Build:  iscc /DAppVersion=0.3.0 packaging\protoprobe.iss   (after python packaging\build.py)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1D8F0E-4C3A-4E55-9D7B-2A5C0F1E9A31}
AppName=ProtoProbe
AppVersion={#AppVersion}
AppPublisher=Pratik Kadam Automation
DefaultDirName={autopf}\ProtoProbe
DefaultGroupName=ProtoProbe
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=ProtoProbe-Setup-{#AppVersion}
SetupIconFile=..\assets\protoprobe.ico
UninstallDisplayIcon={app}\ProtoProbe.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "addtopath"; Description: "Add the plcread command line tool to PATH"; Flags: unchecked

[Files]
Source: "..\dist\ProtoProbe.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\plcread.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\ProtoProbe"; Filename: "{app}\ProtoProbe.exe"
Name: "{group}\Uninstall ProtoProbe"; Filename: "{uninstallexe}"
Name: "{autodesktop}\ProtoProbe"; Filename: "{app}\ProtoProbe.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\ProtoProbe.exe"; Description: "Start ProtoProbe"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsPath(Dir: string): Boolean;
var Path: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then begin Result := True; exit; end;
  Result := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Path) + ';') = 0;
end;
