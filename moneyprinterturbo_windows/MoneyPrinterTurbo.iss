#define MyAppName "MoneyPrinterTurbo"
#define MyAppVersion "1.3.5"
#define MyAppPublisher "MoneyPrinterTurbo"

[Setup]
AppId={{8E96EFD4-4F50-46A0-BA0E-C94F86D4DC58}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MoneyPrinterTurbo
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer-output
OutputBaseFilename=MoneyPrinterTurbo-Windows-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin

[Files]
Source: "..\mpt-package\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\MoneyPrinterTurbo"; Filename: "{cmd}"; Parameters: "/c ""{app}\start.bat"""; WorkingDir: "{app}"
Name: "{autodesktop}\MoneyPrinterTurbo"; Filename: "{cmd}"; Parameters: "/c ""{app}\start.bat"""; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autoprograms}\Update MoneyPrinterTurbo"; Filename: "{cmd}"; Parameters: "/c ""{app}\update.bat"""; WorkingDir: "{app}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{cmd}"; Parameters: "/c ""{app}\start.bat"""; WorkingDir: "{app}"; Description: "Launch MoneyPrinterTurbo"; Flags: nowait postinstall skipifsilent
