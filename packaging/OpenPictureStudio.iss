; Inno Setup script — OpenPictureStudio 内测安装包
; Invoked by build.ps1, which passes:
;   /DAppVersion=<x.y.z>   the version read from src/utils/app_info.py
;   /DRepoRoot=<abs path>  the repository root (so all paths below are root-relative)
;
; Manual run example:
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" ^
;       /DAppVersion=0.17.0 /DRepoRoot="C:\src\OpenPictureStudio" ^
;       packaging\OpenPictureStudio.iss

#if Ver != EncodeVer(6,7,1)
  #error Inno Setup 6.7.1 is required
#endif

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef RepoRoot
  #define RepoRoot ".."
#endif

#define AppName "OpenPictureStudio"
#define AppPublisher "OpenPictureStudio"
#define AppExeName "OpenPictureStudio.exe"
#ifdef ReviewCandidate
  #define BuildSuffix " Candidate"
  #define PackageSuffix "-candidate"
#else
  #define BuildSuffix ""
  #define PackageSuffix ""
#endif

[Setup]
; Fixed AppId so future beta builds upgrade in place instead of installing twice.
AppId={{8F2C4B7A-1D3E-4C9A-9B6F-A0B1C2D3E4F5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}{#BuildSuffix}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName}{#BuildSuffix} Installer

; Per-user install — no admin prompt, friendliest for beta testers.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AlwaysShowDirOnReadyPage=yes
ShowLanguageDialog=yes
UsePreviousLanguage=yes
LanguageDetectionMethod=uilanguage
SetupMutex=Global\OpenPictureStudioMaintenance
CloseApplications=no
RestartApplications=no

SourceDir={#RepoRoot}
OutputDir=dist\installer
OutputBaseFilename={#AppName}-{#AppVersion}{#PackageSuffix}-setup
SetupIconFile=resources\icons\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}{#BuildSuffix}

Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "zh_CN"; MessagesFile: "{#SourcePath}languages\ChineseSimplified.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

#include "installer-messages.iss"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Dirs]
; upgrade.iss creates {app} before Setup reaches it, so Setup would otherwise leave the empty folder behind.
Name: "{app}"; Flags: uninsalwaysuninstall

[Files]
; The manifest stays first: extracting from a solid archive decompresses everything stored before the file.
Source: "dist\OpenPictureStudio\install-manifest.txt"; Flags: dontcopy
; Whole PyInstaller onedir output (exe + _internal + bundled resources). Empty folders stay out of {app}: the
; manifest lists only files, so the next upgrade would reject them as unknown directories.
Source: "dist\OpenPictureStudio\*"; DestDir: "{app}"; Excludes: "install-manifest.txt"; Flags: ignoreversion recursesubdirs; Check: ShouldInstallFile

[InstallDelete]
; The uninstall shortcut is named in the installer's language, and Setup never removes a renamed entry.
Type: files; Name: "{group}\Uninstall {#AppName}.lnk"
Type: files; Name: "{group}\卸载 {#AppName}.lnk"
Type: files; Name: "{group}\Désinstaller {#AppName}.lnk"

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}";   Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";       Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[UninstallDelete]
Type: files; Name: "{app}\install-manifest.txt"

[Code]
#include "running-applications.iss"
#include "upgrade.iss"
