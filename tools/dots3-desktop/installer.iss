#define MyAppName "Dots3 Desktop"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "KPFinInv"
#define MyAppExeName "Dots3Desktop.exe"

[Setup]
AppId={{C49F4C6A-C043-4B12-8B2B-D7316C21A57D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Dots3 Desktop
DefaultGroupName=Dots3 Desktop
DisableProgramGroupPage=yes
OutputDir=installer-output
OutputBaseFilename=Dots3Desktop-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\Dots3Desktop.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Dots3 Desktop"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Dots3 Desktop"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Dots3 Desktop"; Flags: nowait postinstall skipifsilent
