; Inno Setup script: per-user installer (no administrator rights) for Osmo Client.
; Built by packaging\build.ps1:  ISCC /DAppVersion=1.6.5 /O<dist> packaging\installer.iss
; Silent install for a deployment tool:  OsmoClient-<version>-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; Silent uninstall (keeps the user's cameras and files):  unins000.exe /VERYSILENT

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change this GUID: it is how a new version finds and replaces the installed one.
AppId={{8F3C5B0E-6A1D-4E57-9B2A-4C1D7E0A5F31}
AppName=Osmo Client
AppVersion={#AppVersion}
AppVerName=Osmo Client {#AppVersion}
AppPublisher=The Osmo Client authors
AppPublisherURL=https://github.com/guneethind/osmo-client
AppSupportURL=https://github.com/guneethind/osmo-client/issues
AppUpdatesURL=https://github.com/guneethind/osmo-client/releases
VersionInfoVersion={#AppVersion}
; A short path on purpose: the Windows Bluetooth libraries fail to load from long ones.
DefaultDirName={localappdata}\Programs\Osmo Client
DefaultGroupName=Osmo Client
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=OsmoClient-{#AppVersion}-setup
Compression=lzma2/ultra64
SolidCompression=yes
SetupIconFile=..\assets\osmo-client.ico
UninstallDisplayIcon={app}\OsmoClient.exe
LicenseFile=..\LICENSE
WizardStyle=modern
; The running app holds its libraries open: it is closed first (and not started again by Setup itself).
AppMutex=Local\OsmoClient.8091
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start Osmo Client when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\OsmoClient\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Osmo Client"; Filename: "{app}\OsmoClient.exe"
Name: "{group}\Osmo Client (with console)"; Filename: "{app}\OsmoClient-console.exe"
Name: "{group}\Uninstall Osmo Client"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Osmo Client"; Filename: "{app}\OsmoClient.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OsmoClient"; ValueData: """{app}\OsmoClient.exe"" --no-browser"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\OsmoClient.exe"; Description: "Start Osmo Client"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Ask a running copy to stop, so it puts the Wi-Fi back before its files are removed.
Filename: "{sys}\curl.exe"; Parameters: "-s -m 5 -X POST -H ""Origin: http://127.0.0.1:8091"" http://127.0.0.1:8091/api/quit"; Flags: runhidden; RunOnceId: "StopOsmoClient"

[Code]
var
  RemoveData: Boolean;

function InitializeUninstall(): Boolean;
begin
  RemoveData := False;
  if not UninstallSilent() then
    RemoveData := MsgBox('Also remove your linked cameras and settings?' + #13#10 +
      '(The photos and videos you copied from the camera are never removed.)', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveData then
    DelTree(ExpandConstant('{localappdata}\osmo-pc'), True, True, True);
end;
