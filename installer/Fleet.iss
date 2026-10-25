; Build with Inno Setup 6.  The installer is per-user: no admin prompt needed.
; scripts\build-release.ps1 must run first: it produces dist\Fleet\.
#define AppName "Fleet"
#define AppVersion GetEnv("FLEET_VERSION")
#if AppVersion == ""
  #define AppVersion "dev"
#endif
#define BuildRoot GetEnv("FLEET_BUILD_ROOT")
#if BuildRoot == ""
  #define BuildRoot "..\dist\Fleet"
#endif
#define InstallerOutputRoot GetEnv("FLEET_OUTPUT_ROOT")
#if InstallerOutputRoot == ""
  #define InstallerOutputRoot "..\dist"
#endif
#define AppIcon GetEnv("FLEET_APP_ICON")
#if AppIcon == ""
  #define AppIcon "..\build\assets\fleet.ico"
#endif
; Set by CI from the repository it builds from; left blank for local builds so
; the installer never advertises a URL that does not exist.
#define RepoUrl GetEnv("FLEET_REPO_URL")

[Setup]
AppId={{26069E25-E726-47A8-94DB-5DAA6F5CA449}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Fleet contributors
#if RepoUrl != ""
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
#endif
DefaultDirName={localappdata}\Fleet
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#InstallerOutputRoot}
OutputBaseFilename=Fleet-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\Fleet.exe
UninstallDisplayName={#AppName}
; Fleet holds this mutex while its tray is running, so Setup can ask the user to
; quit it instead of failing to replace a locked Fleet.exe.
AppMutex=Local\fleet-tray
CloseApplications=yes
#if FileExists(AppIcon)
SetupIconFile={#AppIcon}
#endif

[Files]
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Fleet"; Filename: "{app}\Fleet.exe"
Name: "{autostartup}\Fleet"; Filename: "{app}\Fleet.exe"; Tasks: startup
Name: "{autoprograms}\Uninstall Fleet"; Filename: "{uninstallexe}"

[Run]
; Remove the legacy recovery task when upgrading; startup is a plain shortcut.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""Fleet Recovery"" /F"; Flags: runhidden waituntilterminated
Filename: "{app}\Fleet.exe"; Description: "Launch Fleet now"; Flags: nowait postinstall skipifsilent

[InstallDelete]
Type: files; Name: "{autostartup}\Fleet.lnk"
Type: files; Name: "{app}\scripts\fleet-guard.ps1"
Type: files; Name: "{app}\scripts\fleet-guard-hidden.vbs"

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""Fleet Recovery"" /F"; Flags: runhidden waituntilterminated

[Tasks]
Name: "startup"; Description: "Start Fleet when I sign in"

; Runtime state is derived from local provider credentials, so uninstalling must
; not leave it behind.
[UninstallDelete]
Type: filesandordirs; Name: "{app}\.cache"
Type: dirifempty; Name: "{app}"
