; AppId and the per-user data directory must stay stable across versions.
#ifndef AppVersion
  #error AppVersion required
#endif
[Setup]
AppId={{1B33068E-450D-4D90-8CD3-7764DB72CF63}
AppName=묑류계
AppVersion={#AppVersion}
AppPublisher=묑류계 프로젝트
DefaultDirName={localappdata}\Programs\Rectifier
DefaultGroupName=묑류계
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=묑류계-Setup-{#AppVersion}-x64
SetupIconFile=..\rectifier\assets\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Rectifier.exe
CloseApplications=no
RestartApplications=no
InfoBeforeFile=사용법.txt

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\묑류계"; Filename: "{app}\Rectifier.exe"
Name: "{group}\묑류계 사용법"; Filename: "{app}\_internal\사용법.txt"

[Run]
Filename: "{app}\Rectifier.exe"; Description: "묑류계 실행"; Flags: nowait postinstall skipifsilent

; No InstallDelete/UninstallDelete: user data and credentials are never removed.
