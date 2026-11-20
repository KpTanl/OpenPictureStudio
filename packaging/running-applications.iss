const
  AppMarker = 'Global\OpenPictureStudioApp';
  ProgramName = 'OpenPictureStudio.exe';
  PluginSwitch = '--ops-plugin-runner';
  WM_CLOSE = $0010;
  GW_OWNER = 4;
  GWL_EXSTYLE = -20;
  WS_EX_TOOLWINDOW = $00000080;
  WS_EX_NOACTIVATE = $08000000;
  RestoreWindow = 9;
  QueryLimited = $1000;
  TerminateAccess = $0001;
  SynchronizeAccess = $00100000;
  ProcessBufferBytes = 65536;
  PollMilliseconds = 500;
  IdentityPolls = 5;
  OrphanPolls = 30;
  SlowClosePolls = 60;

var
  LocalProcesses, OrphanProcesses: array of LongWord;
  MainWindows: array of HWND;
  OtherProcesses: Integer;
  WindowCallback, PollTimer: LongWord;
  RunningPage: TWizardPage;
  DetailsLabel, StatusLabel: TNewStaticText;
  CloseButton, RecheckButton, ForceButton: TNewButton;
  CloseRequested, CloseDenied, OnlyOrphans, PageBusy: Boolean;
  WaitPolls, OrphanPollsSeen, IdentityCountdown: Integer;

function OpenMutex(Access: LongWord; Inherit: BOOL; Name: String): THandle;
  external 'OpenMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';
function EnumProcessIds(Buffer: AnsiString; Size: LongWord; var Needed: LongWord): BOOL;
  external 'K32EnumProcesses@kernel32.dll stdcall';
function OpenProcess(Access: LongWord; Inherit: BOOL; Pid: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function QueryFullProcessImageName(Process: THandle; Flags: LongWord; Buffer: String; var Size: LongWord): BOOL;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall';
function ProcessIdToSessionId(Pid: LongWord; var Session: LongWord): BOOL;
  external 'ProcessIdToSessionId@kernel32.dll stdcall';
function GetCurrentProcessId: LongWord;
  external 'GetCurrentProcessId@kernel32.dll stdcall';
function TerminateProcess(Process: THandle; ExitCode: LongWord): BOOL;
  external 'TerminateProcess@kernel32.dll stdcall';
function EnumWindows(Callback: LongWord; Param: LongInt): BOOL;
  external 'EnumWindows@user32.dll stdcall';
function GetWindowThreadProcessId(Wnd: HWND; var Pid: LongWord): LongWord;
  external 'GetWindowThreadProcessId@user32.dll stdcall';
function IsWindowVisible(Wnd: HWND): BOOL;
  external 'IsWindowVisible@user32.dll stdcall';
function GetWindow(Wnd: HWND; Command: LongWord): HWND;
  external 'GetWindow@user32.dll stdcall';
function GetWindowLong(Wnd: HWND; Index: LongInt): LongInt;
  external 'GetWindowLongW@user32.dll stdcall';
function IsIconic(Wnd: HWND): BOOL;
  external 'IsIconic@user32.dll stdcall';
function ShowProgramWindow(Wnd: HWND; Command: LongInt): BOOL;
  external 'ShowWindow@user32.dll stdcall';
function SetForegroundWindow(Wnd: HWND): BOOL;
  external 'SetForegroundWindow@user32.dll stdcall';
function AllowSetForegroundWindow(Pid: LongWord): BOOL;
  external 'AllowSetForegroundWindow@user32.dll stdcall';
function SetTimer(Wnd: HWND; Id, Milliseconds, Callback: LongWord): LongWord;
  external 'SetTimer@user32.dll stdcall';
function KillTimer(Wnd: HWND; Id: LongWord): BOOL;
  external 'KillTimer@user32.dll stdcall';

{ Inno timestamps each log line, so one line per stage is enough to compare runs recorded with /LOG. }
procedure Stage(Name: String);
begin
  Log('Installer stage: ' + Name);
end;

function MarkerPresent: Boolean;
var Running: THandle;
begin
  Running := OpenMutex($00100000, False, AppMarker);
  Result := Running <> 0;
  if Result then CloseHandle(Running)
  else Result := DLLGetLastError <> 2;
end;

function ImageOf(Pid: LongWord): String;
var Process: THandle; Size: LongWord; Buffer: String;
begin
  Result := '';
  Process := OpenProcess(QueryLimited, False, Pid);
  if Process = 0 then Exit;
  Size := 1024; SetLength(Buffer, Size);
  if QueryFullProcessImageName(Process, 0, Buffer, Size) then Result := Copy(Buffer, 1, Size);
  CloseHandle(Process);
end;

function ProgramPath: String;
begin
  Result := AddBackslash(ExpandConstant('{app}')) + ProgramName;
end;

function IsLocalProcess(Pid: LongWord): Boolean;
var I: Integer;
begin
  Result := False;
  for I := 0 to GetArrayLength(LocalProcesses) - 1 do
    if LocalProcesses[I] = Pid then Result := True;
end;

{ OtherProcesses counts this program elsewhere: another folder, or another Windows session. }
procedure ScanProcesses;
var Buffer: AnsiString; Needed, Pid, Session, OwnSession: LongWord; I, Base, Count: Integer; Image: String;
begin
  SetArrayLength(LocalProcesses, 0); OtherProcesses := 0; Needed := 0;
  SetLength(Buffer, ProcessBufferBytes);
  if not EnumProcessIds(Buffer, ProcessBufferBytes, Needed) then Exit;
  ProcessIdToSessionId(GetCurrentProcessId, OwnSession);
  for I := 0 to Needed div 4 - 1 do begin
    Base := I * 4;
    Pid := Ord(Buffer[Base + 1]) or (Ord(Buffer[Base + 2]) shl 8) or (Ord(Buffer[Base + 3]) shl 16) or (Ord(Buffer[Base + 4]) shl 24);
    Image := ImageOf(Pid);
    if (Image <> '') and (CompareText(ExtractFileName(Image), ProgramName) = 0) then begin
      Session := OwnSession;
      ProcessIdToSessionId(Pid, Session);
      if (CompareText(Image, ProgramPath) = 0) and (Session = OwnSession) then begin
        Count := GetArrayLength(LocalProcesses);
        SetArrayLength(LocalProcesses, Count + 1);
        LocalProcesses[Count] := Pid;
      end else OtherProcesses := OtherProcesses + 1;
    end;
  end;
end;

function ProgramRunning: Boolean;
begin
  ScanProcesses;
  Result := MarkerPresent or (GetArrayLength(LocalProcesses) > 0);
end;

{ A main window is one Alt+Tab would list: visible, no owner, not a tool window, and it can be activated. Dialogs are
  owned, Qt's helper windows are hidden, and the input indicators Windows places inside a process cannot be activated.
  One window per process. }
function VisitWindow(Wnd: HWND; Param: LongInt): BOOL;
var Pid, Other: LongWord; I, Count: Integer;
begin
  Result := True; Pid := 0;
  GetWindowThreadProcessId(Wnd, Pid);
  if not IsLocalProcess(Pid) then Exit;
  if not IsWindowVisible(Wnd) then Exit;
  if GetWindow(Wnd, GW_OWNER) <> 0 then Exit;
  if (GetWindowLong(Wnd, GWL_EXSTYLE) and (WS_EX_TOOLWINDOW or WS_EX_NOACTIVATE)) <> 0 then Exit;
  Count := GetArrayLength(MainWindows);
  for I := 0 to Count - 1 do begin
    Other := 0;
    GetWindowThreadProcessId(MainWindows[I], Other);
    if Other = Pid then Exit;
  end;
  SetArrayLength(MainWindows, Count + 1);
  MainWindows[Count] := Wnd;
end;

procedure ScanWindows;
begin
  SetArrayLength(MainWindows, 0);
  if GetArrayLength(LocalProcesses) = 0 then Exit;
  if WindowCallback = 0 then WindowCallback := CreateCallback(@VisitWindow);
  EnumWindows(WindowCallback, 0);
end;

function IsPluginRunner(CommandLine: String): Boolean;
var Stop: Integer;
begin
  CommandLine := Trim(CommandLine);
  if Copy(CommandLine, 1, 1) = '"' then begin
    Delete(CommandLine, 1, 1);
    Stop := Pos('"', CommandLine);
  end else Stop := Pos(' ', CommandLine);
  if Stop = 0 then CommandLine := ''
  else CommandLine := Trim(Copy(CommandLine, Stop + 1, Length(CommandLine)));
  Result := (CommandLine = PluginSwitch) or (Pos(PluginSwitch + ' ', CommandLine) = 1);
end;

{ Main and plugin processes are the same exe, so only the command line tells them apart.
  Anything that cannot be read counts as unknown, and unknown is never ended here. }
function IdentifyOrphans: Boolean;
var Locator, Service, Items, Item: Variant; I, Count: Integer; Pid: LongWord; Image, CommandLine: String;
begin
  Result := False;
  SetArrayLength(OrphanProcesses, 0);
  if (OtherProcesses > 0) or (GetArrayLength(LocalProcesses) = 0) then Exit;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process WHERE Name = ''' + ProgramName + '''');
    for I := 0 to Items.Count - 1 do begin
      Item := Items.ItemIndex(I);
      Pid := Item.ProcessId;
      if VarIsNull(Item.ExecutablePath) or VarIsNull(Item.CommandLine) then Exit;
      Image := Item.ExecutablePath; CommandLine := Item.CommandLine;
      if (CompareText(Image, ProgramPath) <> 0) or not IsLocalProcess(Pid) or not IsPluginRunner(CommandLine) then Exit;
      Count := GetArrayLength(OrphanProcesses);
      SetArrayLength(OrphanProcesses, Count + 1);
      OrphanProcesses[Count] := Pid;
    end;
    Result := GetArrayLength(OrphanProcesses) = GetArrayLength(LocalProcesses);
  except
    Result := False;
  end;
end;

function EndOrphans: Boolean;
var I: Integer; Process: THandle;
begin
  Result := True;
  for I := 0 to GetArrayLength(OrphanProcesses) - 1 do begin
    Process := OpenProcess(TerminateAccess or SynchronizeAccess, False, OrphanProcesses[I]);
    if Process = 0 then Result := False
    else begin
      if TerminateProcess(Process, 1) then begin
        if WaitForSingleObject(Process, 5000) <> 0 then Result := False;
      end else Result := False;
      CloseHandle(Process);
    end;
  end;
end;

procedure StopPolling;
begin
  if PollTimer <> 0 then KillTimer(0, PollTimer);
  PollTimer := 0;
end;

procedure SetRunningStatus(Text: String);
begin
  if StatusLabel.Caption <> Text then StatusLabel.Caption := Text;
end;

procedure TrackOrphans;
begin
  if (GetArrayLength(MainWindows) > 0) or (GetArrayLength(LocalProcesses) = 0) then begin
    OnlyOrphans := False; OrphanPollsSeen := 0; IdentityCountdown := 0;
    Exit;
  end;
  if IdentityCountdown <= 0 then begin
    OnlyOrphans := IdentifyOrphans;
    IdentityCountdown := IdentityPolls;
  end;
  IdentityCountdown := IdentityCountdown - 1;
  if OnlyOrphans then OrphanPollsSeen := OrphanPollsSeen + 1 else OrphanPollsSeen := 0;
end;

procedure ShowRunningState(Polling: Boolean);
var Local: Integer;
begin
  Local := GetArrayLength(LocalProcesses);
  DetailsLabel.Caption := FmtMessage(CustomMessage('OpsCloseDetails'), [IntToStr(Local + OtherProcesses), ExpandConstant('{app}')]);
  if Polling then TrackOrphans;
  CloseButton.Enabled := GetArrayLength(MainWindows) > 0;
  if CloseRequested then CloseButton.Caption := CustomMessage('OpsCloseAgainButton');
  ForceButton.Visible := OrphanPollsSeen >= OrphanPolls;
  if ForceButton.Visible then SetRunningStatus(FmtMessage(CustomMessage('OpsCloseOrphan'), [IntToStr(Local)]))
  else if (OtherProcesses > 0) or (Local = 0) then SetRunningStatus(CustomMessage('OpsCloseOther'))
  else if GetArrayLength(MainWindows) = 0 then SetRunningStatus(CustomMessage('OpsCloseNoWindow'))
  else if CloseDenied then SetRunningStatus(CustomMessage('OpsCloseDenied'))
  else if not CloseRequested then SetRunningStatus('')
  else if WaitPolls >= SlowClosePolls then SetRunningStatus(CustomMessage('OpsCloseSlow'))
  else SetRunningStatus(CustomMessage('OpsCloseWaiting'));
end;

procedure RefreshRunningPage(Polling: Boolean);
var Advance: Boolean;
begin
  if PageBusy then Exit;
  Advance := False;
  PageBusy := True;
  try
    if Polling and CloseRequested then WaitPolls := WaitPolls + 1;
    if ProgramRunning then begin
      ScanWindows;
      ShowRunningState(Polling);
      WizardForm.NextButton.Enabled := False;
    end else begin
      if PollTimer <> 0 then Stage('program closed');
      StopPolling;
      SetArrayLength(MainWindows, 0);
      OrphanPollsSeen := 0;
      ShowRunningState(False);
      CloseButton.Enabled := False;
      SetRunningStatus(CustomMessage('OpsCloseClear'));
      WizardForm.NextButton.Enabled := True;
      Advance := CloseRequested;
    end;
  finally
    PageBusy := False;
  end;
  if Advance then WizardForm.NextButton.OnClick(WizardForm.NextButton);
end;

procedure PollRunningPage(Wnd: HWND; Message, Id, Time: LongWord);
begin
  try RefreshRunningPage(True); except StopPolling; end;
end;

procedure EnterRunningPage;
begin
  Stage('waiting for the program to close');
  CloseDenied := False; OrphanPollsSeen := 0; IdentityCountdown := 0; WaitPolls := 0;
  WizardForm.NextButton.Enabled := False;
  if PollTimer = 0 then PollTimer := SetTimer(0, 0, PollMilliseconds, CreateCallback(@PollRunningPage));
  RefreshRunningPage(False);
end;

procedure CloseButtonClick(Sender: TObject);
var I: Integer; Pid: LongWord;
begin
  ScanProcesses; ScanWindows;
  CloseDenied := False;
  for I := 0 to GetArrayLength(MainWindows) - 1 do begin
    Pid := 0;
    GetWindowThreadProcessId(MainWindows[I], Pid);
    if IsIconic(MainWindows[I]) then ShowProgramWindow(MainWindows[I], RestoreWindow);
    AllowSetForegroundWindow(Pid);
    SetForegroundWindow(MainWindows[I]);
    if not PostMessage(MainWindows[I], WM_CLOSE, 0, 0) then CloseDenied := True;
  end;
  CloseRequested := True; WaitPolls := 0;
  RefreshRunningPage(False);
end;

procedure RecheckButtonClick(Sender: TObject);
begin
  WaitPolls := 0;
  RefreshRunningPage(False);
end;

procedure ForceButtonClick(Sender: TObject);
begin
  PageBusy := True;
  try
    if MsgBox(FmtMessage(CustomMessage('OpsForceConfirm'), [IntToStr(GetArrayLength(LocalProcesses))]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then begin
      ScanProcesses; ScanWindows;
      if (GetArrayLength(MainWindows) > 0) or not IdentifyOrphans or not EndOrphans then
        MsgBox(CustomMessage('OpsForceFailed'), mbError, MB_OK);
    end;
  finally
    PageBusy := False;
  end;
  OrphanPollsSeen := 0; IdentityCountdown := 0; ForceButton.Visible := False;
  RefreshRunningPage(False);
end;

function NewRunningLabel(Top, Height: Integer): TNewStaticText;
begin
  Result := TNewStaticText.Create(RunningPage);
  Result.Parent := RunningPage.Surface;
  Result.AutoSize := False;
  Result.WordWrap := True;
  Result.SetBounds(0, Top, RunningPage.SurfaceWidth, Height);
end;

function NewRunningButton(Caption: String; Left, Top: Integer; OnClick: TNotifyEvent): TNewButton;
begin
  Result := TNewButton.Create(RunningPage);
  Result.Parent := RunningPage.Surface;
  Result.Caption := Caption;
  Result.SetBounds(Left, Top, WizardForm.CalculateButtonWidth([Caption]), WizardForm.NextButton.Height);
  Result.OnClick := OnClick;
end;

procedure CreateRunningPage(After: Integer);
var HintLabel: TNewStaticText; Top, ButtonLeft, CloseWidth: Integer;
begin
  RunningPage := CreateCustomPage(After, CustomMessage('OpsCloseTitle'), CustomMessage('OpsCloseDescription'));
  HintLabel := NewRunningLabel(0, ScaleY(50));
  HintLabel.Caption := CustomMessage('OpsCloseHint');
  DetailsLabel := NewRunningLabel(ScaleY(54), ScaleY(44));
  Top := ScaleY(102);
  CloseWidth := WizardForm.CalculateButtonWidth([CustomMessage('OpsCloseButton'), CustomMessage('OpsCloseAgainButton')]);
  CloseButton := NewRunningButton(CustomMessage('OpsCloseButton'), 0, Top, @CloseButtonClick);
  CloseButton.Width := CloseWidth;
  ButtonLeft := CloseWidth + ScaleX(8);
  RecheckButton := NewRunningButton(CustomMessage('OpsRecheckButton'), ButtonLeft, Top, @RecheckButtonClick);
  ButtonLeft := ButtonLeft + RecheckButton.Width + ScaleX(8);
  ForceButton := NewRunningButton(CustomMessage('OpsForceButton'), ButtonLeft, Top, @ForceButtonClick);
  ForceButton.Visible := False;
  Top := Top + WizardForm.NextButton.Height + ScaleY(10);
  StatusLabel := NewRunningLabel(Top, RunningPage.SurfaceHeight - Top);
end;
