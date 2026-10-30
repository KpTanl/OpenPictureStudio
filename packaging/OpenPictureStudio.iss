; Inno Setup script — OpenPictureStudio 内测安装包
; Invoked by build.ps1, which passes:
;   /DAppVersion=<x.y.z>   the version read from src/utils/app_info.py
;   /DRepoRoot=<abs path>  the repository root (so all paths below are root-relative)
;
; Manual run example:
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" ^
;       /DAppVersion=0.17.0 /DRepoRoot="C:\src\OpenPictureStudio" ^
;       packaging\OpenPictureStudio.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef RepoRoot
  #define RepoRoot ".."
#endif

#define AppName "OpenPictureStudio"
#define AppPublisher "OpenPictureStudio"
#define AppExeName "OpenPictureStudio.exe"

[Setup]
; Fixed AppId so future beta builds upgrade in place instead of installing twice.
AppId={{8F2C4B7A-1D3E-4C9A-9B6F-A0B1C2D3E4F5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion} (Beta)
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Beta Installer

; Per-user install — no admin prompt, friendliest for beta testers.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AppMutex=Global\OpenPictureStudioApp
SetupMutex=Global\OpenPictureStudioMaintenance
CloseApplications=no
RestartApplications=no

SourceDir={#RepoRoot}
OutputDir=dist\installer
OutputBaseFilename={#AppName}-{#AppVersion}-beta-setup
SetupIconFile=resources\icons\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion} (Beta)

Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Whole PyInstaller onedir output (exe + _internal + bundled resources).
Source: "dist\OpenPictureStudio\*"; DestDir: "{app}"; Excludes: "install-manifest.txt"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\OpenPictureStudio\install-manifest.txt"; Flags: dontcopy

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}";   Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";       Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[UninstallDelete]
Type: files; Name: "{app}\install-manifest.txt"

[Code]
#include "upgrade.iss"
