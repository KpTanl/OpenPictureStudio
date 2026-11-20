const
  MaintenanceMarker = 'Global\OpenPictureStudioMaintenance';
  AdmissionMarker = 'Global\OpenPictureStudioAdmission';
  ManifestName = 'install-manifest.txt';
  PendingName = '.install-pending.txt';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F2C4B7A-1D3E-4C9A-9B6F-A0B1C2D3E4F5}_is1';
  MaxAppPath = 140;

type
  TProgramFile = record
    Name: String;
    Hash: String;
    Seen: Boolean;
    Actual: String;
    SizeHigh, SizeLow, TimeHigh, TimeLow: LongWord;
  end;
  TProgramFiles = array of TProgramFile;

var
  OldFiles, NewFiles: TProgramFiles;
  OldLines, NewLines: TArrayOfString;
  OldVersion, NewManifestHash, RegisteredVersion, PreflightVersion, Uninstaller, ConfirmedDowngrade, FirstMismatch: String;
  MaintenanceHandle: THandle;
  Repairing, Prepared, Completed, LegacyRegistrationOnly, RepairSelected, BaselineFromPackage, RebuildUninstallLog: Boolean;
  VerificationPage: TOutputProgressWizardPage;
  ModePage: TInputOptionWizardPage;
  Verifying, VerificationCancelled, CollectMismatches: Boolean;
  Verified: array of Boolean;
  FilesSeen, FirstRoundFiles, Mismatches: Integer;

function CreateMutexEx(Attributes: LongWord; Name: String; Flags, Access: LongWord): THandle;
  external 'CreateMutexExW@kernel32.dll stdcall';
function ReleaseMutex(Handle: THandle): BOOL;
  external 'ReleaseMutex@kernel32.dll stdcall';
function ConvertSecurity(Text: String; Revision: LongWord; var Descriptor: LongWord; Size: LongWord): BOOL;
  external 'ConvertStringSecurityDescriptorToSecurityDescriptorW@advapi32.dll stdcall';
function SetKernelObjectSecurity(Handle: THandle; Information: LongWord; Descriptor: LongWord): BOOL;
  external 'SetKernelObjectSecurity@advapi32.dll stdcall';
function LocalFree(Memory: LongWord): LongWord;
  external 'LocalFree@kernel32.dll stdcall';
function MoveFileEx(Existing, Destination: String; Flags: LongWord): BOOL;
  external 'MoveFileExW@kernel32.dll stdcall';
function GetFileAttributes(Name: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function PathIsDirectoryEmpty(Name: String): BOOL;
  external 'PathIsDirectoryEmptyW@shlwapi.dll stdcall';

procedure Fail(Name: String);
begin
  RaiseException(FmtMessage(CustomMessage(Name), []));
end;

procedure FailWith(Name, Value: String);
begin
  RaiseException(FmtMessage(CustomMessage(Name), [Value]));
end;

function SharedMutex(Name: String; Exclusive: Boolean): THandle;
var Descriptor: LongWord;
begin
  Result := OpenMutex($00100001, False, Name);
  if Result <> 0 then begin
    if Exclusive then begin CloseHandle(Result); Fail('OpsBusy'); end;
    Exit;
  end;
  if DLLGetLastError <> 2 then Fail('OpsProtectionInspect');
  Result := CreateMutexEx(0, Name, 0, $00160001);
  if (Result <> 0) and (DLLGetLastError = 183) then begin
    if Exclusive then begin CloseHandle(Result); Fail('OpsBusy'); end;
  end;
  if Result = 0 then begin
    Result := OpenMutex($00100001, False, Name);
    if Result = 0 then Fail('OpsProtectionCreate');
    if Exclusive then begin CloseHandle(Result); Fail('OpsBusy'); end;
    Exit;
  end;
  Descriptor := 0;
  if not ConvertSecurity('D:P(A;;0x00100001;;;WD)', 1, Descriptor, 0) then begin
    CloseHandle(Result);
    Fail('OpsProtectionPermissions');
  end;
  try
    if not SetKernelObjectSecurity(Result, 4, Descriptor) then begin
      CloseHandle(Result);
      Fail('OpsProtectionPermissions');
    end;
  finally
    LocalFree(Descriptor);
  end;
end;

function AcquireAdmission(Name: String): THandle;
var WaitResult: LongWord;
begin
  Result := SharedMutex(Name, False);
  WaitResult := WaitForSingleObject(Result, 5000);
  if (WaitResult <> 0) and (WaitResult <> $80) then begin
    CloseHandle(Result);
    Fail('OpsProtectionWait');
  end;
end;

procedure CheckRunning(Name: String);
var Running: THandle;
begin
  Running := OpenMutex($00100000, False, Name);
  if Running <> 0 then begin
    CloseHandle(Running);
    Fail('OpsRunning');
  end;
  if DLLGetLastError <> 2 then Fail('OpsRunningUnknown');
end;

procedure CheckApplication;
var Gate: THandle;
begin
  Gate := AcquireAdmission(AdmissionMarker);
  try
    CheckRunning(AppMarker);
    ScanProcesses;
    if GetArrayLength(LocalProcesses) > 0 then Fail('OpsRunning');
  finally
    ReleaseMutex(Gate); CloseHandle(Gate);
  end;
end;

procedure CheckPlain(Name: String);
var Attributes: LongWord; Current, Parent: String;
begin
  Current := ExpandFileName(Name);
  repeat
    Attributes := GetFileAttributes(Current);
    if Attributes = $FFFFFFFF then begin
      if (DLLGetLastError <> 2) and (DLLGetLastError <> 3) then FailWith('OpsInspect', Current);
    end else if (Attributes and $400) <> 0 then FailWith('OpsLinked', Current);
    Parent := ExtractFileDir(Current);
    if Parent = Current then Break;
    Current := Parent;
  until Current = '';
end;

function NormalPath(Name: String): String;
begin
  Result := Lowercase(RemoveBackslashUnlessRoot(ExpandFileName(Name)));
end;

function Overlaps(Left, Right: String): Boolean;
begin
  Left := AddBackslash(NormalPath(Left));
  Right := AddBackslash(NormalPath(Right));
  Result := (Pos(Left, Right) = 1) or (Pos(Right, Left) = 1);
end;

function Hexadecimal(Value: String; Count: Integer): Boolean;
var I: Integer;
begin
  Result := Length(Value) = Count;
  if not Result then Exit;
  for I := 1 to Length(Value) do
    if Pos(Lowercase(Value[I]), '0123456789abcdef') = 0 then begin Result := False; Exit; end;
end;

function Numeric(Value: String): Boolean;
var I: Integer;
begin
  Result := (Length(Value) > 0) and (Length(Value) <= 18);
  if not Result then Exit;
  for I := 1 to Length(Value) do
    if (Value[I] < '0') or (Value[I] > '9') then begin Result := False; Exit; end;
end;

function VersionNumbers(Value: String): TArrayOfString;
var I: Integer; Part: String;
begin
  SetArrayLength(Result, 0);
  Part := '';
  for I := 1 to Length(Value) + 1 do begin
    if (I > Length(Value)) or (Value[I] = '.') then begin
      if (not Numeric(Part)) or (Length(Part) > 9) then FailWith('OpsVersionUnknown', Value);
      SetArrayLength(Result, GetArrayLength(Result) + 1);
      Result[GetArrayLength(Result) - 1] := Part;
      Part := '';
    end else Part := Part + Value[I];
  end;
  if GetArrayLength(Result) <> 3 then FailWith('OpsVersionUnknown', Value);
end;

function CompareVersions(Left, Right: String): Integer;
var A, B: TArrayOfString; I: Integer;
begin
  A := VersionNumbers(Left); B := VersionNumbers(Right); Result := 0;
  for I := 0 to 2 do begin
    if StrToInt(A[I]) < StrToInt(B[I]) then begin Result := -1; Exit; end;
    if StrToInt(A[I]) > StrToInt(B[I]) then begin Result := 1; Exit; end;
  end;
end;

function FindProgramFile(var Files: TProgramFiles; Name: String): Integer;
var Left, Right, Middle, Comparison: Integer;
begin
  Result := -1; Left := 0; Right := GetArrayLength(Files) - 1;
  while Left <= Right do begin
    Middle := Left + (Right - Left) div 2;
    Comparison := CompareText(Files[Middle].Name, Name);
    if Comparison = 0 then begin Result := Middle; Exit; end;
    if Comparison < 0 then Left := Middle + 1 else Right := Middle - 1;
  end;
end;

procedure SortProgramFiles(var Files: TProgramFiles; Left, Right: Integer);
var I, J: Integer; Pivot: String; Temporary: TProgramFile;
begin
  I := Left; J := Right; Pivot := Files[Left + (Right - Left) div 2].Name;
  repeat
    while CompareText(Files[I].Name, Pivot) < 0 do I := I + 1;
    while CompareText(Files[J].Name, Pivot) > 0 do J := J - 1;
    if I <= J then begin
      Temporary := Files[I]; Files[I] := Files[J]; Files[J] := Temporary;
      I := I + 1; J := J - 1;
    end;
  until I > J;
  if Left < J then SortProgramFiles(Files, Left, J);
  if I < Right then SortProgramFiles(Files, I, Right);
end;

{ Every path under a directory sits together in the sorted list, starting at the lower bound of its prefix. }
function HasPrefix(var Files: TProgramFiles; Prefix: String): Boolean;
var Left, Right, Middle: Integer;
begin
  Left := 0; Right := GetArrayLength(Files);
  while Left < Right do begin
    Middle := Left + (Right - Left) div 2;
    if CompareText(Files[Middle].Name, Prefix) < 0 then Left := Middle + 1 else Right := Middle;
  end;
  Result := False;
  if Left < GetArrayLength(Files) then
    Result := CompareText(Copy(Files[Left].Name, 1, Length(Prefix)), Prefix) = 0;
end;

procedure ReadManifest(Lines: TArrayOfString; var Files: TProgramFiles; var Version: String);
var I, J, A, B: Integer; Line, Name, Part, Digest, Executable: String; Parts: TArrayOfString;
begin
  SetArrayLength(Files, 0);
  if GetArrayLength(Lines) < 4 then Fail('OpsManifestDamaged');
  Executable := 'OpenPictureStudio.exe';
  if Lines[0] <> 'OPS-INSTALL' + #9 + '1' then Fail('OpsManifestUnsupported');
  if (Pos('version' + #9, Lines[1]) <> 1)
      or (Pos('source' + #9, Lines[2]) <> 1) or not Hexadecimal(Copy(Lines[2], 8, 40), 40)
      or (Length(Lines[2]) <> 47) then Fail('OpsManifestUnsupported');
  Version := Copy(Lines[1], 9, Length(Lines[1]));
  Parts := VersionNumbers(Version);
  for I := 3 to GetArrayLength(Lines) - 1 do begin
    Line := Lines[I]; A := Pos(#9, Line);
    if A <> 65 then Fail('OpsManifestEntry');
    Digest := Copy(Line, 1, 64);
    Delete(Line, 1, A); B := Pos(#9, Line);
    if (B = 0) or not Hexadecimal(Digest, 64) or not Numeric(Copy(Line, 1, B - 1)) then Fail('OpsManifestEntry');
    Name := Copy(Line, B + 1, Length(Line));
    if (Name = '') or (Name[1] = '\') or (Pos('/', Name) <> 0) or (Pos(':', Name) <> 0)
        or (CompareText(Name, ManifestName) = 0) or (Pos('.install-', Lowercase(Name)) = 1)
        or (Pos('unins', Lowercase(Name)) = 1) then FailWith('OpsManifestPath', Name);
    Part := '';
    for J := 1 to Length(Name) + 1 do begin
      if (J > Length(Name)) or (Name[J] = '\') then begin
        if (Part = '') or (Part = '.') or (Part = '..') or (Part[Length(Part)] = '.')
            or (Part[Length(Part)] = ' ') then FailWith('OpsManifestPath', Name);
        Part := '';
      end else begin
        if (Ord(Name[J]) < 32) or (Pos(Name[J], '*?"<>|') <> 0) then FailWith('OpsManifestPath', Name);
        Part := Part + Name[J];
      end;
    end;
    J := GetArrayLength(Files); SetArrayLength(Files, J + 1);
    Files[J].Name := Name; Files[J].Hash := Digest;
  end;
  if GetArrayLength(Files) > 1 then SortProgramFiles(Files, 0, GetArrayLength(Files) - 1);
  for I := 1 to GetArrayLength(Files) - 1 do
    if CompareText(Files[I - 1].Name, Files[I].Name) = 0 then FailWith('OpsManifestDuplicate', Files[I].Name);
  if FindProgramFile(Files, Executable) < 0 then Fail('OpsManifestNoProgram');
end;

procedure ReadIdentity;
var Root, OtherRoot: Integer; Directory: String;
begin
  if IsAdminInstallMode then begin Root := HKLM64; OtherRoot := HKCU; end
  else begin Root := HKCU; OtherRoot := HKLM64; end;
  RegisteredVersion := ''; Uninstaller := '';
  if RegQueryStringValue(OtherRoot, UninstallKey, 'InstallLocation', Directory) then
    if NormalPath(Directory) = NormalPath(ExpandConstant('{app}')) then
      Fail('OpsTwoModes');
  if RegKeyExists(Root, UninstallKey) then begin
    if not RegQueryStringValue(Root, UninstallKey, 'InstallLocation', Directory)
        or not RegQueryStringValue(Root, UninstallKey, 'DisplayVersion', RegisteredVersion)
        or not RegQueryStringValue(Root, UninstallKey, 'UninstallString', Uninstaller) then
      Fail('OpsIdentity');
    if NormalPath(Directory) <> NormalPath(ExpandConstant('{app}')) then
      Fail('OpsDirectoryMismatch');
    Uninstaller := RemoveQuotes(Uninstaller);
    if NormalPath(ExtractFileDir(Uninstaller)) <> NormalPath(ExpandConstant('{app}')) then
      Fail('OpsUninstallerLocation');
  end;
end;

function RecoveryLines: TArrayOfString;
var I: Integer; Mode: String;
begin
  Mode := 'user'; if IsAdminInstallMode then Mode := 'machine64';
  SetArrayLength(Result, GetArrayLength(OldLines) + 5);
  Result[0] := 'OPS-PENDING' + #9 + '1'; Result[1] := 'target' + #9 + NewManifestHash;
  Result[2] := 'mode' + #9 + Mode; Result[3] := 'root' + #9 + NormalPath(ExpandConstant('{app}'));
  Result[4] := 'phase' + #9 + 'installing';
  for I := 0 to GetArrayLength(OldLines) - 1 do Result[I + 5] := OldLines[I];
end;

function SameLines(Actual, Expected: TArrayOfString): Boolean;
var I: Integer;
begin
  Result := False;
  if GetArrayLength(Actual) <> GetArrayLength(Expected) then Exit;
  for I := 0 to GetArrayLength(Actual) - 1 do if Actual[I] <> Expected[I] then Exit;
  Result := True;
end;

function MatchesLines(Name: String; Expected: TArrayOfString): Boolean;
var Actual: TArrayOfString;
begin
  Result := False;
  if LoadStringsFromFile(Name, Actual) then Result := SameLines(Actual, Expected);
end;

function UninstallLogUsable: Boolean;
var Data: AnsiString; Id, AppId: String; I, Bit: Integer; Crc, StoredCrc: LongWord;
begin
  Result := False;
  if Uninstaller = '' then Exit;
  if not FileExists(Uninstaller) then Exit;
  if not LoadStringFromFile(ChangeFileExt(Uninstaller, '.dat'), Data) then Exit;
  if Length(Data) < 448 then Exit;
  Id := 'Inno Setup Uninstall Log (b) 64-bit';
  while Length(Id) < 64 do Id := Id + #0;
  if Copy(Data, 1, 64) <> Id then Exit;
  AppId := Copy(UninstallKey, Pos('{', UninstallKey), 38);
  if Copy(Data, 65, Length(AppId) + 1) <> AppId + #0 then Exit;
  Crc := $FFFFFFFF;
  for I := 1 to 444 do begin
    Crc := Crc xor Ord(Data[I]);
    for Bit := 0 to 7 do
      if (Crc and 1) <> 0 then Crc := (Crc shr 1) xor $EDB88320
      else Crc := Crc shr 1;
  end;
  Crc := Crc xor $FFFFFFFF;
  StoredCrc := Ord(Data[445]) or (Ord(Data[446]) shl 8) or (Ord(Data[447]) shl 16) or (Ord(Data[448]) shl 24);
  Result := (StoredCrc = Crc) or (StoredCrc = $11111111);
end;

function OwnedMetadata(Name: String): Boolean;
begin
  Result := Repairing and (CompareText(Name, PendingName) = 0);
  if CompareText(Name, ManifestName) = 0 then
    Result := BaselineFromPackage or MatchesLines(ExpandConstant('{app}\') + Name, OldLines)
        or (Repairing and MatchesLines(ExpandConstant('{app}\') + Name, NewLines));
  if (CompareText(Name, ManifestName + '.new') = 0) and Repairing then
    Result := MatchesLines(ExpandConstant('{app}\') + Name, NewLines);
  if CompareText(Name, PendingName + '.new') = 0 then
    Result := MatchesLines(ExpandConstant('{app}\') + Name, RecoveryLines);
  if Uninstaller <> '' then Result := Result or (CompareText(Name, ExtractFileName(Uninstaller)) = 0)
      or (CompareText(Name, ChangeFileExt(ExtractFileName(Uninstaller), '.dat')) = 0)
      or (CompareText(Name, ChangeFileExt(ExtractFileName(Uninstaller), '.msg')) = 0);
end;

procedure ShowInstallStatus(Text: String);
begin
  WizardForm.StatusLabel.Caption := Text;
  WizardForm.FilenameLabel.Caption := '';
  WizardForm.StatusLabel.Refresh;
end;

procedure ShowInstallFile(Name: String);
begin
  WizardForm.FilenameLabel.Caption := Name;
  WizardForm.FilenameLabel.Refresh;
end;

procedure Remember(var Entry: TProgramFile; var Item: TFindRec; Hash: String);
begin
  Entry.Seen := True; Entry.Actual := Hash;
  Entry.SizeHigh := Item.SizeHigh; Entry.SizeLow := Item.SizeLow;
  Entry.TimeHigh := Item.LastWriteTime.dwHighDateTime; Entry.TimeLow := Item.LastWriteTime.dwLowDateTime;
end;

{ Nothing runs between the rounds and the maintenance marker is held, so Standard compares what the file system
  already reports. Repair hashes again because it is about to trust what the first round found. }
procedure Recheck(var Entry: TProgramFile; var Item: TFindRec; Name: String);
begin
  if not Entry.Seen then FailWith('OpsTreeChanged', Name);
  if RepairSelected then begin
    ShowInstallFile(Name);
    if CompareText(GetSHA256OfFile(AddBackslash(ExpandConstant('{app}')) + Name), Entry.Actual) <> 0 then
      FailWith('OpsTreeChanged', Name);
  end else if (Entry.SizeHigh <> Item.SizeHigh) or (Entry.SizeLow <> Item.SizeLow)
      or (Entry.TimeHigh <> Item.LastWriteTime.dwHighDateTime) or (Entry.TimeLow <> Item.LastWriteTime.dwLowDateTime) then
    FailWith('OpsTreeChanged', Name);
end;

procedure CheckFile(Name: String; var Item: TFindRec; Rechecking: Boolean);
var Hash: String; OldIndex, NewIndex, Total: Integer; Known: Boolean;
begin
  if Verifying then begin
    Total := GetArrayLength(NewFiles);
    if GetArrayLength(OldFiles) > Total then Total := GetArrayLength(OldFiles);
    VerificationPage.SetText(CustomMessage('OpsVerifying'), Name);
    VerificationPage.SetProgress(FilesSeen, Total);
    if VerificationCancelled then Fail('OpsVerificationCancelled');
  end;
  OldIndex := FindProgramFile(OldFiles, Name);
  NewIndex := FindProgramFile(NewFiles, Name);
  if (OldIndex < 0) and (NewIndex < 0) then FailWith('OpsUnknownFile', Name);
  FilesSeen := FilesSeen + 1;
  if Rechecking then begin
    if NewIndex >= 0 then Recheck(NewFiles[NewIndex], Item, Name)
    else Recheck(OldFiles[OldIndex], Item, Name);
    Exit;
  end;
  Known := False;
  Hash := GetSHA256OfFile(AddBackslash(ExpandConstant('{app}')) + Name);
  if OldIndex >= 0 then Known := CompareText(Hash, OldFiles[OldIndex].Hash) = 0;
  if Repairing and (NewIndex >= 0) then Known := Known or (CompareText(Hash, NewFiles[NewIndex].Hash) = 0);
  if not Known and not RepairSelected then begin
    if not CollectMismatches then FailWith('OpsModifiedFile', Name);
    if Mismatches = 0 then FirstMismatch := Name;
    Mismatches := Mismatches + 1;
  end;
  if NewIndex >= 0 then begin
    Verified[NewIndex] := (Uninstaller <> '') and not RebuildUninstallLog and (OldIndex >= 0)
        and (CompareText(Hash, NewFiles[NewIndex].Hash) = 0);
    Remember(NewFiles[NewIndex], Item, Hash);
  end else Remember(OldFiles[OldIndex], Item, Hash);
end;

procedure WalkTree(Relative: String; Rechecking: Boolean);
var Item: TFindRec; Name: String;
begin
  if FindFirst(AddBackslash(ExpandConstant('{app}')) + Relative + '*', Item) then try
    repeat
      if (Item.Name <> '.') and (Item.Name <> '..') then begin
        Name := Relative + Item.Name;
        if (Item.Attributes and $400) <> 0 then FailWith('OpsLinked', AddBackslash(ExpandConstant('{app}')) + Name);
        if (Item.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then begin
          if not HasPrefix(OldFiles, Name + '\') and not HasPrefix(NewFiles, Name + '\') then
            FailWith('OpsUnknownDirectory', Name);
          WalkTree(Name + '\', Rechecking);
        end else if not OwnedMetadata(Name) then CheckFile(Name, Item, Rechecking);
      end;
    until not FindNext(Item);
  finally FindClose(Item); end;
end;

procedure CheckTree(Rechecking: Boolean);
begin
  CheckPlain(ExpandConstant('{app}'));
  if DirExists(ExpandConstant('{app}')) then WalkTree('', Rechecking);
end;

procedure CheckLegacyDirectory;
var Name: String; Attributes, Error: LongWord;
begin
  Name := ExpandConstant('{app}');
  CheckPlain(Name);
  Attributes := GetFileAttributes(Name);
  if Attributes = $FFFFFFFF then begin
    Error := DLLGetLastError;
    if (Error = 2) or (Error = 3) then Exit;
    FailWith('OpsInspectOldDirectory', Name);
  end;
  if not PathIsDirectoryEmpty(Name) then
    FailWith('OpsManifestMissing', Name);
end;

function InstalledManifestUsable: Boolean;
var Name: String;
begin
  Result := False;
  Name := ExpandConstant('{app}\') + ManifestName;
  if not FileExists(Name) then Exit;
  CheckPlain(Name);
  try
    if LoadStringsFromFile(Name, OldLines) then begin
      ReadManifest(OldLines, OldFiles, OldVersion);
      Result := True;
    end;
  except
    SetArrayLength(OldLines, 0); SetArrayLength(OldFiles, 0); OldVersion := '';
  end;
end;

{ Repair of the installed version can trust the manifest inside this installer instead of the lost one. }
procedure UsePackageBaseline;
begin
  if CompareVersions(RegisteredVersion, '{#AppVersion}') <> 0 then FailWith('OpsRepairNeedsSameVersion', RegisteredVersion);
  OldLines := NewLines; OldFiles := NewFiles; OldVersion := RegisteredVersion;
  BaselineFromPackage := True;
end;

procedure ReadBaseline;
var Lines: TArrayOfString; I: Integer; Mode: String;
begin
  SetArrayLength(OldLines, 0); SetArrayLength(OldFiles, 0); OldVersion := '';
  LegacyRegistrationOnly := False; BaselineFromPackage := False;
  Repairing := FileExists(ExpandConstant('{app}\') + PendingName);
  Mode := 'user'; if IsAdminInstallMode then Mode := 'machine64';
  if Repairing then begin
    CheckPlain(ExpandConstant('{app}\') + PendingName);
    if not LoadStringsFromFile(ExpandConstant('{app}\') + PendingName, Lines) then Fail('OpsRecoveryRead');
    if GetArrayLength(Lines) < 5 then Fail('OpsRecoveryDamaged');
    if (Lines[0] <> 'OPS-PENDING' + #9 + '1') or (Lines[1] <> 'target' + #9 + NewManifestHash)
        or (Lines[2] <> 'mode' + #9 + Mode) or (CompareText(Lines[3], 'root' + #9 + NormalPath(ExpandConstant('{app}'))) <> 0)
        or (Lines[4] <> 'phase' + #9 + 'installing') then
      Fail('OpsRecoveryMismatch');
    SetArrayLength(OldLines, GetArrayLength(Lines) - 5);
    for I := 5 to GetArrayLength(Lines) - 1 do OldLines[I - 5] := Lines[I];
    if GetArrayLength(OldLines) > 0 then ReadManifest(OldLines, OldFiles, OldVersion);
    BaselineFromPackage := (RegisteredVersion = '{#AppVersion}') and SameLines(OldLines, NewLines);
  end else if FileExists(ExpandConstant('{app}\') + ManifestName) then begin
    if RegisteredVersion = '' then Fail('OpsManifestVersion');
    if not InstalledManifestUsable or (RegisteredVersion <> OldVersion) then begin
      if not RepairSelected then Fail('OpsManifestUseRepair');
      UsePackageBaseline;
    end;
  end else if RegisteredVersion <> '' then begin
    if RepairSelected then UsePackageBaseline
    else begin
      CheckLegacyDirectory;
      LegacyRegistrationOnly := True;
    end;
  end;
end;

procedure SkipSpace(Text: String; var Position: Integer);
begin
  while (Position <= Length(Text)) and (Ord(Text[Position]) <= 32) do Position := Position + 1;
end;

function JsonString(Text: String; var Position: Integer): String;
var C: Char; Code, I, Digit: Integer;
begin
  Result := '';
  if Text[Position] <> '"' then Fail('OpsStorageRead');
  Position := Position + 1;
  while Position <= Length(Text) do begin
    C := Text[Position]; Position := Position + 1;
    if C = '"' then Exit;
    if Ord(C) < 32 then Fail('OpsStorageInvalid');
    if C = '\' then begin
      if Position > Length(Text) then Fail('OpsStorageInvalid');
      C := Text[Position]; Position := Position + 1;
      case C of
        '"', '\', '/': Result := Result + C;
        'b': Result := Result + #8;
        'f': Result := Result + #12;
        'n': Result := Result + #10;
        'r': Result := Result + #13;
        't': Result := Result + #9;
        'u': begin
          Code := 0;
          for I := 1 to 4 do begin
            if Position > Length(Text) then Fail('OpsStorageInvalid');
            Digit := Pos(Lowercase(Text[Position]), '0123456789abcdef') - 1;
            if Digit < 0 then Fail('OpsStorageInvalid');
            Code := Code * 16 + Digit; Position := Position + 1;
          end;
          Result := Result + Chr(Code);
        end;
      else Fail('OpsStorageInvalid'); end;
    end else Result := Result + C;
  end;
  Fail('OpsStorageInvalid');
end;

{ The top-level "results" list is history only; every other path still blocks. }
procedure CheckJsonValue(Text: String; var Position: Integer; Depth: Integer; CheckPaths: Boolean);
var C, Ending: Char; Value: String; Start: Integer; Nested: Boolean;
begin
  if Depth > 64 then Fail('OpsStorageInvalid');
  SkipSpace(Text, Position);
  if Position > Length(Text) then Fail('OpsStorageInvalid');
  C := Text[Position];
  if C = '"' then begin
    Value := JsonString(Text, Position);
    StringChangeEx(Value, '/', '\', True);
    if CheckPaths and (((Length(Value) > 2) and (Copy(Value, 2, 2) = ':\')) or (Copy(Value, 1, 2) = '\\')) then
      if Overlaps(Value, ExpandConstant('{app}')) then FailWith('OpsStorageConflict', Value);
  end else if (C = '{') or (C = '[') then begin
    Ending := ']'; if C = '{' then Ending := '}';
    Position := Position + 1; SkipSpace(Text, Position);
    if (Position <= Length(Text)) and (Text[Position] = Ending) then begin Position := Position + 1; Exit; end;
    repeat
      Nested := CheckPaths;
      if C = '{' then begin
        SkipSpace(Text, Position);
        if Position > Length(Text) then Fail('OpsStorageInvalid');
        Value := JsonString(Text, Position);
        SkipSpace(Text, Position);
        if (Position > Length(Text)) or (Text[Position] <> ':') then Fail('OpsStorageInvalid');
        Position := Position + 1;
        if (Depth = 0) and (Value = 'results') then Nested := False;
      end;
      CheckJsonValue(Text, Position, Depth + 1, Nested); SkipSpace(Text, Position);
      if Position > Length(Text) then Fail('OpsStorageInvalid');
      if Text[Position] = Ending then begin Position := Position + 1; Exit; end;
      if Text[Position] <> ',' then Fail('OpsStorageInvalid');
      Position := Position + 1;
    until False;
  end else begin
    Start := Position;
    while (Position <= Length(Text)) and (Pos(Text[Position], ',}] ' + #9 + #10 + #13) = 0) do Position := Position + 1;
    Value := Copy(Text, Start, Position - Start);
    if (Value <> 'true') and (Value <> 'false') and (Value <> 'null') then begin
      if Value = '' then Fail('OpsStorageInvalid');
      for Start := 1 to Length(Value) do
        if Pos(Value[Start], '0123456789.eE+-') = 0 then Fail('OpsStorageInvalid');
    end;
  end;
end;

procedure CheckStorageFile(Name: String);
var Lines: TArrayOfString; Text: String; I, Position: Integer;
begin
  CheckPlain(Name);
  if not FileExists(Name) then Exit;
  if not LoadStringsFromFile(Name, Lines) then Fail('OpsStorageRead');
  Text := '';
  for I := 0 to GetArrayLength(Lines) - 1 do Text := Text + Lines[I] + #10;
  Position := 1; CheckJsonValue(Text, Position, 0, True); SkipSpace(Text, Position);
  if Position <= Length(Text) then Fail('OpsStorageInvalid');
end;

procedure CheckStorage;
begin
  CheckStorageFile(ExpandConstant('{localappdata}\OpenPictureStudio\OpenPictureStudio\.storage\storage.json'));
end;

procedure AtomicLines(Name: String; Lines: TArrayOfString);
var Temporary: String;
begin
  CheckPlain(Name); Temporary := Name + '.new'; CheckPlain(Temporary);
  if FileExists(Temporary) then begin
    if not MatchesLines(Temporary, Lines) then FailWith('OpsMetadataConflict', Temporary);
  end else if not SaveStringsToUTF8File(Temporary, Lines, False) then Fail('OpsMetadataSave');
  if not MoveFileEx(Temporary, Name, 9) then FailWith('OpsMetadataPublish', Name);
end;

function ExistingInstallation: Boolean;
begin
  Result := False;
  try
    ReadIdentity;
    Result := (RegisteredVersion <> '') and not FileExists(ExpandConstant('{app}\') + PendingName);
  except
    { PrepareToInstall reports this with the installer's other checks. }
  end;
end;

function ShouldInstallFile: Boolean;
var Name: String; Index: Integer;
begin
  Result := True;
  { Setup also asks here before the manifests are read, and possibly before the app constant is usable. }
  if not Prepared then Exit;
  Name := ExpandConstant(CurrentFileName);
  Delete(Name, 1, Length(AddBackslash(ExpandConstant('{app}'))));
  Index := FindProgramFile(NewFiles, Name);
  if Index >= 0 then Result := not Verified[Index];
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfoInfo, MemoTypeInfoInfo, MemoComponentsInfoInfo, MemoGroupInfoInfo, MemoTasksInfoInfo: String): String;
begin
  Result := '';
  try
    ReadIdentity;
    if RegisteredVersion = '' then Result := FmtMessage(CustomMessage('OpsReadyNew'), ['{#AppVersion}'])
    else begin
      if CompareVersions(RegisteredVersion, '{#AppVersion}') = 0 then Result := FmtMessage(CustomMessage('OpsReadyRepair'), ['{#AppVersion}'])
      else Result := FmtMessage(CustomMessage('OpsReadyUpgrade'), [RegisteredVersion, '{#AppVersion}']);
      if RepairSelected then Result := Result + NewLine + CustomMessage('OpsReadyThorough')
      else Result := Result + NewLine + CustomMessage('OpsReadyStandard');
    end;
  except
    { PrepareToInstall reports this with the installer's other checks. }
  end;
  Result := Result + NewLine + MemoDirInfoInfo + NewLine + MemoTasksInfoInfo;
end;

procedure ConfirmRepair;
begin
  if WizardSilent then FailWith('OpsModifiedFile', FirstMismatch);
  if MsgBox(FmtMessage(CustomMessage('OpsAskRepair'), [IntToStr(Mismatches), FirstMismatch]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then
    Fail('OpsRepairDeclined');
  RepairSelected := True;
  ModePage.Values[1] := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Version, Failure: String;
begin
  Result := ''; Prepared := False;
  try
    CheckApplication;
    ReadIdentity;
    if (RegisteredVersion = '') and (Length(ExpandConstant('{app}')) > MaxAppPath) then
      FailWith('OpsPathTooLong', IntToStr(MaxAppPath));
    CheckPlain(ExpandConstant('{app}'));
    CheckStorage;
    Stage('reading the new manifest');
    ExtractTemporaryFile(ManifestName);
    NewManifestHash := GetSHA256OfFile(ExpandConstant('{tmp}\') + ManifestName);
    if not LoadStringsFromFile(ExpandConstant('{tmp}\') + ManifestName, NewLines) then Fail('OpsManifestReadNew');
    ReadManifest(NewLines, NewFiles, Version);
    if Version <> '{#AppVersion}' then Fail('OpsVersionMismatch');
    ReadBaseline;
    RebuildUninstallLog := (Uninstaller <> '') and not UninstallLogUsable;
    if (RegisteredVersion = '') and not Repairing then RepairSelected := False;
    SetArrayLength(Verified, 0);
    SetArrayLength(Verified, GetArrayLength(NewFiles));
    FilesSeen := 0; Mismatches := 0; FirstMismatch := '';
    CollectMismatches := (RegisteredVersion <> '') or Repairing;
    VerificationCancelled := False; Verifying := True;
    Stage('checking program files');
    VerificationPage.Show;
    { Hide runs CurPageChanged, and a script event discards an exception still pending in a finally block. }
    Failure := '';
    try CheckTree(False); except Failure := GetExceptionMessage; end;
    Verifying := False; CollectMismatches := False; VerificationPage.Hide;
    if Failure <> '' then RaiseException(Failure);
    FirstRoundFiles := FilesSeen;
    Stage('checked ' + IntToStr(FilesSeen) + ' files, ' + IntToStr(Mismatches) + ' differ');
    if Mismatches > 0 then ConfirmRepair;
    if (RegisteredVersion <> '') and (CompareVersions(RegisteredVersion, '{#AppVersion}') > 0) then begin
      if WizardSilent then Fail('OpsSilentDowngrade');
      if ConfirmedDowngrade <> RegisteredVersion then begin
        if MsgBox(FmtMessage(CustomMessage('OpsDowngradeConfirm'), [RegisteredVersion, '{#AppVersion}']), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then
          Fail('OpsDowngradeCancelled');
        ConfirmedDowngrade := RegisteredVersion;
      end;
    end;
    PreflightVersion := RegisteredVersion;
    Prepared := True;
  except Result := GetExceptionMessage; end;
end;

function SkippedFiles: Integer;
var I: Integer;
begin
  Result := 0;
  for I := 0 to GetArrayLength(Verified) - 1 do
    if Verified[I] then Result := Result + 1;
end;

procedure BeforeFileCopy;
var I: Integer; Name: String;
begin
  if not Prepared then Fail('OpsPreflightIncomplete');
  Stage('second check');
  CheckApplication; ReadIdentity;
  if RegisteredVersion <> PreflightVersion then Fail('OpsVersionChanged');
  if RebuildUninstallLog <> ((Uninstaller <> '') and not UninstallLogUsable) then Fail('OpsTreeCount');
  if LegacyRegistrationOnly then CheckLegacyDirectory;
  CheckStorage;
  FilesSeen := 0;
  if RepairSelected then ShowInstallStatus(CustomMessage('OpsVerifying'));
  CheckTree(True);
  if FilesSeen <> FirstRoundFiles then Fail('OpsTreeCount');
  if not ForceDirectories(ExpandConstant('{app}')) then Fail('OpsCreateDirectory');
  if not Repairing then begin
    AtomicLines(ExpandConstant('{app}\') + PendingName, RecoveryLines);
    Repairing := True;
  end;
  if RebuildUninstallLog then
    for I := 0 to 2 do begin
      Name := Uninstaller;
      if I = 1 then Name := ChangeFileExt(Name, '.dat');
      if I = 2 then Name := ChangeFileExt(Name, '.msg');
      CheckPlain(Name);
      if FileExists(Name) then
        if not DeleteFile(Name) then FailWith('OpsRemoveOld', Name);
    end;
  Stage('copying ' + IntToStr(GetArrayLength(NewFiles) - SkippedFiles) + ' files, skipping ' + IntToStr(SkippedFiles));
end;

procedure AfterFileCopy;
var I, Index: Integer; Name: String;
begin
  Stage('verifying new files');
  ShowInstallStatus(CustomMessage('OpsVerifyingNew'));
  for I := 0 to GetArrayLength(NewFiles) - 1 do begin
    ShowInstallFile(NewFiles[I].Name);
    Name := ExpandConstant('{app}\') + NewFiles[I].Name; CheckPlain(Name);
    if not FileExists(Name) or (CompareText(GetSHA256OfFile(Name), NewFiles[I].Hash) <> 0) then
      FailWith('OpsVerificationFailed', NewFiles[I].Name);
  end;
  Stage('removing old files');
  for I := 0 to GetArrayLength(OldFiles) - 1 do begin
    Index := FindProgramFile(NewFiles, OldFiles[I].Name);
    Name := ExpandConstant('{app}\') + OldFiles[I].Name;
    if (Index < 0) and FileExists(Name) then begin
      CheckPlain(Name);
      if not RepairSelected and (CompareText(GetSHA256OfFile(Name), OldFiles[I].Hash) <> 0) then FailWith('OpsOldFileChanged', Name);
      if not DeleteFile(Name) then FailWith('OpsRemoveOld', Name);
      Name := ExtractFileDir(Name);
      while NormalPath(Name) <> NormalPath(ExpandConstant('{app}')) do begin
        if not RemoveDir(Name) then Break;
        Name := ExtractFileDir(Name);
      end;
    end;
  end;
  AtomicLines(ExpandConstant('{app}\') + ManifestName, NewLines);
  if not DeleteFile(ExpandConstant('{app}\') + PendingName) then Fail('OpsFinishFailed');
  Completed := True;
  Stage('finished');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  try
    if CurStep = ssInstall then BeforeFileCopy;
    if CurStep = ssPostInstall then AfterFileCopy;
  except
    SuppressibleMsgBox(GetExceptionMessage, mbCriticalError, MB_OK, IDOK);
    Abort;
  end;
end;

function RepairRequested: Boolean;
var I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/REPAIR') = 0 then Result := True;
end;

procedure InitializeWizard;
begin
  VerificationPage := CreateOutputProgressPage(CustomMessage('OpsVerificationTitle'), CustomMessage('OpsVerificationDescription'));
  ModePage := CreateInputOptionPage(wpSelectTasks, CustomMessage('OpsModeTitle'), CustomMessage('OpsModeDescription'), CustomMessage('OpsModePrompt'), True, False);
  ModePage.Add(CustomMessage('OpsModeStandard'));
  ModePage.Add(CustomMessage('OpsModeRepair'));
  RepairSelected := RepairRequested;
  ModePage.Values[0] := not RepairSelected;
  ModePage.Values[1] := RepairSelected;
  CreateRunningPage(ModePage.ID);
  Stage('wizard ready');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = ModePage.ID then Result := not ExistingInstallation;
  if PageID = RunningPage.ID then Result := WizardSilent or not ProgramRunning;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and (Length(WizardDirValue) > MaxAppPath) then begin
    SuppressibleMsgBox(FmtMessage(CustomMessage('OpsPathTooLong'), [IntToStr(MaxAppPath)]), mbError, MB_OK, IDOK);
    Result := False;
  end;
  if CurPageID = ModePage.ID then RepairSelected := ModePage.Values[1];
  if CurPageID = RunningPage.ID then Result := not ProgramRunning;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = RunningPage.ID then EnterRunningPage else StopPolling;
  if CurPageID = wpFinished then begin
    if Completed then WizardForm.FinishedLabel.Caption := CustomMessage('OpsFinished')
    else begin
      WizardForm.FinishedHeadingLabel.Caption := CustomMessage('OpsUnfinishedTitle');
      WizardForm.FinishedLabel.Caption := CustomMessage('OpsUnfinished');
    end;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 1;
  if Completed then Result := 0;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if Verifying then begin
    VerificationCancelled := True; Cancel := False; Confirm := False;
  end else if CurPageID = RunningPage.ID then begin
    PageBusy := True;
    Cancel := ExitSetupMsgBox; Confirm := False;
    PageBusy := False;
  end;
end;

procedure DeinitializeSetup;
begin
  StopPolling;
end;

function InitializeUninstall: Boolean;
begin
  Result := False;
  try
    MaintenanceHandle := SharedMutex(MaintenanceMarker, True);
    if FileExists(ExpandConstant('{app}\') + PendingName) then Fail('OpsUninstallRepairFirst');
    CheckApplication;
    Result := True;
  except
    SuppressibleMsgBox(GetExceptionMessage, mbError, MB_OK, IDOK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Version: String;
begin
  if CurUninstallStep = usUninstall then
    try
      CheckApplication; CheckStorage;
      ReadIdentity;
      if not LoadStringsFromFile(ExpandConstant('{app}\') + ManifestName, OldLines) then Fail('OpsUninstallVerify');
      ReadManifest(OldLines, OldFiles, Version);
      SetArrayLength(NewFiles, 0); Repairing := False;
      CheckTree(False);
    except
      SuppressibleMsgBox(GetExceptionMessage, mbCriticalError, MB_OK, IDOK);
      Abort;
    end;
end;

procedure DeinitializeUninstall;
begin
  if MaintenanceHandle <> 0 then CloseHandle(MaintenanceHandle);
end;
