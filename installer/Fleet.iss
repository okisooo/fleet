; Build with Inno Setup 6.  The installer is per-user: no admin prompt needed.
#define AppName "Fleet"
#define AppVersion GetEnv("FLEET_VERSION")
#if AppVersion == ""
  #define AppVersion "dev"
#endif

[Setup]
AppId={{26069E25-E726-47A8-94DB-5DAA6F5CA449}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Fleet
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Fleet-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\Fleet.exe

[Files]
Source: "..\dist\Fleet.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autostartup}\Fleet"; Filename: "{app}\Fleet.exe"; Tasks: startup
Name: "{autoprograms}\Fleet"; Filename: "{app}\Fleet.exe"
Name: "{autoprograms}\Uninstall Fleet"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Fleet.exe"; Description: "Launch Fleet now"; Flags: nowait postinstall skipifsilent

[Tasks]
Name: "startup"; Description: "Start Fleet when I sign in"; Flags: unchecked
