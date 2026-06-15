; ===========================================================================
;  Tranqli — Inno Setup installer script  (revision 3, clean)
;
;  Compile with:
;     "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" tranqli.iss
;
;  Source folder: dist\Tranqli\  (PyInstaller --onedir output — build first.)
; ===========================================================================

#define MyAppName        "Tranqli"
#define MyAppVersion     "0.1.0"
#define MyAppPublisher   "martins"
#define MyAppURL         "https://github.com/martins-fyi/tranqli"
#define MyAppExeName     "Tranqli.exe"

[Setup]
; AppId — Inno Setup convention is {{GUID} (two opening braces, ONE closing).
; The {{ escapes to a literal { in the registry; the final } is taken literally
; without escape. Net result in the uninstall key: {GUID}_is1. DO NOT add a
; second closing brace — that produces {GUID}}_is1 which Add/Remove Programs
; can't parse correctly.
AppId={{8B4F2D1E-7C3A-4E59-A1B2-3D4E5F678901}

AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; Per-user install, no elevation dialog. The absence of
; PrivilegesRequiredOverridesAllowed is what removes the "install for current
; user / all users" prompt that appears BEFORE the wizard.
DefaultDirName={userpf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest

LicenseFile=LICENSE

OutputDir=installer
OutputBaseFilename=Tranqli-Setup-{#MyAppVersion}

SetupIconFile=green_tracker\assets\Tranqli.ico

Compression=lzma2/ultra
SolidCompression=yes

WizardStyle=modern

UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startup"; Description: "Start {#MyAppName} automatically when Windows starts"; GroupDescription: "Startup:"

[Files]
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User data in %APPDATA%\Tranqli\ is INTENTIONALLY preserved on uninstall.
