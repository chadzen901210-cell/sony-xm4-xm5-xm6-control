; Inno Setup script for the XM Control installer. Run packaging/build.ps1 first, then:
;   iscc /DAppVersion=0.1.0 /DRepoUrl=https://github.com/<owner>/<repo> packaging\installer.iss
; Installs for the current user only (no administrator rights needed).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef RepoUrl
  #define RepoUrl ""
#endif
#ifndef DistDir
  #define DistDir "..\dist"
#endif
#define AppName "XM Control"
#define AppExe "XM Control.exe"

[Setup]
AppId={{6F3B2A57-2C1E-4C8B-9A7E-58D1C3E0A4B2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=The XM Control contributors
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#DistDir}
OutputBaseFilename=XM-Control-{#AppVersion}-setup
SetupIconFile=..\sonyxm\web\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "nl"; MessagesFile: "compiler:Languages\Dutch.isl"
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#DistDir}\XM Control\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; The app keeps running in the notification area after its window closes
Filename: "{sys}\taskkill.exe"; Parameters: "/IM ""{#AppExe}"" /F"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
; Window profile and settings (sonyxm/webui.py data_dir)
Type: filesandordirs; Name: "{localappdata}\XMControl"
