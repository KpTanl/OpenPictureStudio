const
  AppMarker = 'Global\OpenPictureStudioApp';
  MaintenanceMarker = 'Global\OpenPictureStudioMaintenance';
  AdmissionMarker = 'Global\OpenPictureStudioAdmission';
  ManifestName = 'install-manifest.txt';
  PendingName = '.install-pending.txt';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F2C4B7A-1D3E-4C9A-9B6F-A0B1C2D3E4F5}_is1';

type
  TProgramFile = record
    Name: String;
    Hash: String;
  end;
  TProgramFiles = array of TProgramFile;

var
  OldFiles, NewFiles: TProgramFiles;
  OldLines, NewLines: TArrayOfString;
  OldVersion, NewManifestHash, RegisteredVersion, PreflightVersion, Uninstaller, ConfirmedDowngrade: String;
  MaintenanceHandle: THandle;
  Repairing, Prepared, Completed, LegacyRegistrationOnly: Boolean;
  VerificationPage: TOutputProgressWizardPage;
  Verifying, VerificationCancelled: Boolean;

function OpenMutex(Access: LongWord; Inherit: BOOL; Name: String): THandle;
  external 'OpenMutexW@kernel32.dll stdcall';
function CreateMutexEx(Attributes: LongWord; Name: String; Flags, Access: LongWord): THandle;
  external 'CreateMutexExW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: THandle): BOOL;
  external 'ReleaseMutex@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';
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

procedure Fail(Message: String);
begin
  RaiseException(Message);
end;

function SharedMutex(Name: String; Exclusive: Boolean): THandle;
var Descriptor: LongWord;
begin
  Result := OpenMutex($00100001, False, Name);
  if Result <> 0 then begin
    if Exclusive then begin CloseHandle(Result); Fail('Another installation or removal is in progress.'); end;
    Exit;
  end;
  if DLLGetLastError <> 2 then Fail('Cannot inspect installation protection. Close other installers and try again.');
  Result := CreateMutexEx(0, Name, 0, $00160001);
  if (Result <> 0) and (DLLGetLastError = 183) then begin
    if Exclusive then begin CloseHandle(Result); Fail('Another installation or removal is in progress.'); end;
  end;
  if Result = 0 then begin
    Result := OpenMutex($00100001, False, Name);
    if Result = 0 then Fail('Cannot create installation protection.');
    if Exclusive then begin CloseHandle(Result); Fail('Another installation or removal is in progress.'); end;
    Exit;
  end;
  Descriptor := 0;
  if not ConvertSecurity('D:P(A;;0x00100001;;;WD)', 1, Descriptor, 0) then begin
    CloseHandle(Result);
    Fail('Cannot prepare installation protection permissions.');
  end;
  try
    if not SetKernelObjectSecurity(Result, 4, Descriptor) then begin
      CloseHandle(Result);
      Fail('Cannot set installation protection permissions.');
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
    Fail('Cannot obtain installation protection. Try again.');
  end;
end;

procedure CheckRunning(Name: String);
var Running: THandle;
begin
  Running := OpenMutex($00100000, False, Name);
  if Running <> 0 then begin
    CloseHandle(Running);
    Fail('OpenPictureStudio or an earlier version is running. Close all instances, including other signed-in users, then try again.');
  end;
  if DLLGetLastError <> 2 then Fail('Cannot check running applications. No files have been changed.');
end;

procedure CheckApplication;
var Gate: THandle;
begin
  Gate := AcquireAdmission(AdmissionMarker);
  try
    CheckRunning(AppMarker);
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
      if (DLLGetLastError <> 2) and (DLLGetLastError <> 3) then Fail('Cannot inspect: ' + Current);
    end else if (Attributes and $400) <> 0 then Fail('A linked path cannot be updated: ' + Current);
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
      if (not Numeric(Part)) or (Length(Part) > 9) then Fail('Unrecognized installed version: ' + Value);
      SetArrayLength(Result, GetArrayLength(Result) + 1);
      Result[GetArrayLength(Result) - 1] := Part;
      Part := '';
    end else Part := Part + Value[I];
  end;
  if GetArrayLength(Result) <> 3 then Fail('Unrecognized installed version: ' + Value);
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

procedure ReadManifest(Lines: TArrayOfString; var Files: TProgramFiles; var Version: String);
var I, J, A, B: Integer; Line, Name, Part, Digest, Executable: String; Parts: TArrayOfString;
begin
  SetArrayLength(Files, 0);
  if GetArrayLength(Lines) < 4 then Fail('Missing or damaged installation manifest. Back up your data and use a clean installation.');
  Executable := 'OpenPictureStudio.exe';
  if Lines[0] <> 'OPS-INSTALL' + #9 + '1' then Fail('Unsupported installation manifest.');
  if (Pos('version' + #9, Lines[1]) <> 1)
      or (Pos('source' + #9, Lines[2]) <> 1) or not Hexadecimal(Copy(Lines[2], 8, 40), 40)
      or (Length(Lines[2]) <> 47) then Fail('Unsupported installation manifest.');
  Version := Copy(Lines[1], 9, Length(Lines[1]));
  Parts := VersionNumbers(Version);
  for I := 3 to GetArrayLength(Lines) - 1 do begin
    Line := Lines[I]; A := Pos(#9, Line);
    if A <> 65 then Fail('Invalid manifest entry.');
    Digest := Copy(Line, 1, 64);
    Delete(Line, 1, A); B := Pos(#9, Line);
    if (B = 0) or not Hexadecimal(Digest, 64) or not Numeric(Copy(Line, 1, B - 1)) then Fail('Invalid manifest entry.');
    Name := Copy(Line, B + 1, Length(Line));
    if (Name = '') or (Name[1] = '\') or (Pos('/', Name) <> 0) or (Pos(':', Name) <> 0)
        or (CompareText(Name, ManifestName) = 0) or (Pos('.install-', Lowercase(Name)) = 1)
        or (Pos('unins', Lowercase(Name)) = 1) then Fail('Unsafe manifest path: ' + Name);
    Part := '';
    for J := 1 to Length(Name) + 1 do begin
      if (J > Length(Name)) or (Name[J] = '\') then begin
        if (Part = '') or (Part = '.') or (Part = '..') or (Part[Length(Part)] = '.')
            or (Part[Length(Part)] = ' ') then Fail('Unsafe manifest path: ' + Name);
        Part := '';
      end else begin
        if (Ord(Name[J]) < 32) or (Pos(Name[J], '*?"<>|') <> 0) then Fail('Unsafe manifest path: ' + Name);
        Part := Part + Name[J];
      end;
    end;
    J := GetArrayLength(Files); SetArrayLength(Files, J + 1);
    Files[J].Name := Name; Files[J].Hash := Digest;
  end;
  if GetArrayLength(Files) > 1 then SortProgramFiles(Files, 0, GetArrayLength(Files) - 1);
  for I := 1 to GetArrayLength(Files) - 1 do
    if CompareText(Files[I - 1].Name, Files[I].Name) = 0 then Fail('Duplicate manifest path: ' + Files[I].Name);
  if FindProgramFile(Files, Executable) < 0 then Fail('The program executable is absent from the manifest.');
end;

procedure ReadIdentity;
var Root, OtherRoot: Integer; Directory: String;
begin
  if IsAdminInstallMode then begin Root := HKLM64; OtherRoot := HKCU; end
  else begin Root := HKCU; OtherRoot := HKLM64; end;
  RegisteredVersion := ''; Uninstaller := '';
  if RegQueryStringValue(OtherRoot, UninstallKey, 'InstallLocation', Directory) then
    if NormalPath(Directory) = NormalPath(ExpandConstant('{app}')) then
      Fail('Two installation modes share this directory. Back up your data and separate the installations first.');
  if RegKeyExists(Root, UninstallKey) then begin
    if not RegQueryStringValue(Root, UninstallKey, 'InstallLocation', Directory)
        or not RegQueryStringValue(Root, UninstallKey, 'DisplayVersion', RegisteredVersion)
        or not RegQueryStringValue(Root, UninstallKey, 'UninstallString', Uninstaller) then
      Fail('Cannot identify the existing installation.');
    if NormalPath(Directory) <> NormalPath(ExpandConstant('{app}')) then
      Fail('The selected directory does not match the existing installation.');
    Uninstaller := RemoveQuotes(Uninstaller);
    if NormalPath(ExtractFileDir(Uninstaller)) <> NormalPath(ExpandConstant('{app}')) then
      Fail('Invalid existing uninstaller location.');
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

function MatchesLines(Name: String; Expected: TArrayOfString): Boolean;
var Actual: TArrayOfString; I: Integer;
begin
  Result := False;
  if not LoadStringsFromFile(Name, Actual) then Exit;
  if GetArrayLength(Actual) <> GetArrayLength(Expected) then Exit;
  for I := 0 to GetArrayLength(Actual) - 1 do if Actual[I] <> Expected[I] then Exit;
  Result := True;
end;

function OwnedMetadata(Name: String): Boolean;
begin
  Result := Repairing and (CompareText(Name, PendingName) = 0);
  if CompareText(Name, ManifestName) = 0 then
    Result := MatchesLines(ExpandConstant('{app}\') + Name, OldLines)
        or (Repairing and MatchesLines(ExpandConstant('{app}\') + Name, NewLines));
  if (CompareText(Name, ManifestName + '.new') = 0) and Repairing then
    Result := MatchesLines(ExpandConstant('{app}\') + Name, NewLines);
  if CompareText(Name, PendingName + '.new') = 0 then
    Result := MatchesLines(ExpandConstant('{app}\') + Name, RecoveryLines);
  if Uninstaller <> '' then Result := Result or (CompareText(Name, ExtractFileName(Uninstaller)) = 0)
      or (CompareText(Name, ChangeFileExt(ExtractFileName(Uninstaller), '.dat')) = 0)
      or (CompareText(Name, ChangeFileExt(ExtractFileName(Uninstaller), '.msg')) = 0);
end;

procedure CheckTree(Relative: String);
var Item: TFindRec; Name, FullName, Hash: String; OldIndex, NewIndex, I: Integer; Known: Boolean;
begin
  FullName := AddBackslash(ExpandConstant('{app}')) + Relative;
  CheckPlain(FullName);
  if not DirExists(FullName) then Exit;
  if FindFirst(AddBackslash(FullName) + '*', Item) then try
    repeat
      if (Item.Name <> '.') and (Item.Name <> '..') then begin
        Name := Relative + Item.Name;
        CheckPlain(AddBackslash(ExpandConstant('{app}')) + Name);
        if (Item.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then begin
          Known := False;
          for I := 0 to GetArrayLength(OldFiles) - 1 do
            if Pos(Lowercase(Name + '\'), Lowercase(OldFiles[I].Name)) = 1 then Known := True;
          for I := 0 to GetArrayLength(NewFiles) - 1 do
            if Pos(Lowercase(Name + '\'), Lowercase(NewFiles[I].Name)) = 1 then Known := True;
          if not Known then Fail('Back up and move this unrecognized directory before continuing: ' + Name);
          CheckTree(Name + '\');
        end else if not OwnedMetadata(Name) then begin
          if Verifying then begin
            VerificationPage.SetText('Verifying program files', Name);
            VerificationPage.SetProgress(0, 0);
            if VerificationCancelled then Fail('Verification cancelled. No program files have been changed.');
          end;
          OldIndex := FindProgramFile(OldFiles, Name);
          NewIndex := FindProgramFile(NewFiles, Name);
          Known := False;
          Hash := GetSHA256OfFile(AddBackslash(ExpandConstant('{app}')) + Name);
          if OldIndex >= 0 then Known := CompareText(Hash, OldFiles[OldIndex].Hash) = 0;
          if Repairing and (NewIndex >= 0) then Known := Known or (CompareText(Hash, NewFiles[NewIndex].Hash) = 0);
          if not Known then Fail('Back up and move this unrecognized or modified file before continuing: ' + Name);
        end;
      end;
    until not FindNext(Item);
  finally FindClose(Item); end;
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
    Fail('Cannot inspect the old installation directory: ' + Name);
  end;
  if not PathIsDirectoryEmpty(Name) then
    Fail('This older beta has no installation manifest. Back up your data, close the application, and uninstall it manually before installing. Old installation directory: ' + Name);
end;

procedure ReadBaseline;
var Lines: TArrayOfString; I: Integer; Mode: String;
begin
  SetArrayLength(OldLines, 0); SetArrayLength(OldFiles, 0); OldVersion := '';
  LegacyRegistrationOnly := False;
  Repairing := FileExists(ExpandConstant('{app}\') + PendingName);
  Mode := 'user'; if IsAdminInstallMode then Mode := 'machine64';
  if Repairing then begin
    CheckPlain(ExpandConstant('{app}\') + PendingName);
    if not LoadStringsFromFile(ExpandConstant('{app}\') + PendingName, Lines) then Fail('Cannot read installation recovery record.');
    if GetArrayLength(Lines) < 5 then Fail('Damaged installation recovery record.');
    if (Lines[0] <> 'OPS-PENDING' + #9 + '1') or (Lines[1] <> 'target' + #9 + NewManifestHash)
        or (Lines[2] <> 'mode' + #9 + Mode) or (CompareText(Lines[3], 'root' + #9 + NormalPath(ExpandConstant('{app}'))) <> 0)
        or (Lines[4] <> 'phase' + #9 + 'installing') then
      Fail('Run the same installer in the original installation mode to repair this installation.');
    SetArrayLength(OldLines, GetArrayLength(Lines) - 5);
    for I := 5 to GetArrayLength(Lines) - 1 do OldLines[I - 5] := Lines[I];
    if GetArrayLength(OldLines) > 0 then ReadManifest(OldLines, OldFiles, OldVersion);
  end else if FileExists(ExpandConstant('{app}\') + ManifestName) then begin
    CheckPlain(ExpandConstant('{app}\') + ManifestName);
    if not LoadStringsFromFile(ExpandConstant('{app}\') + ManifestName, OldLines) then Fail('Cannot read the old installation manifest.');
    ReadManifest(OldLines, OldFiles, OldVersion);
    if (RegisteredVersion = '') or (RegisteredVersion <> OldVersion) then Fail('The installed version and manifest do not match.');
  end else if RegisteredVersion <> '' then begin
    CheckLegacyDirectory;
    LegacyRegistrationOnly := True;
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
  if Text[Position] <> '"' then Fail('Cannot read storage configuration.');
  Position := Position + 1;
  while Position <= Length(Text) do begin
    C := Text[Position]; Position := Position + 1;
    if C = '"' then Exit;
    if Ord(C) < 32 then Fail('Invalid storage configuration.');
    if C = '\' then begin
      if Position > Length(Text) then Fail('Invalid storage configuration.');
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
            if Position > Length(Text) then Fail('Invalid storage configuration.');
            Digit := Pos(Lowercase(Text[Position]), '0123456789abcdef') - 1;
            if Digit < 0 then Fail('Invalid storage configuration.');
            Code := Code * 16 + Digit; Position := Position + 1;
          end;
          Result := Result + Chr(Code);
        end;
      else Fail('Invalid storage configuration.'); end;
    end else Result := Result + C;
  end;
  Fail('Invalid storage configuration.');
end;

{ The top-level "results" list is history only; every other path still blocks. }
procedure CheckJsonValue(Text: String; var Position: Integer; Depth: Integer; CheckPaths: Boolean);
var C, Ending: Char; Value: String; Start: Integer; Nested: Boolean;
begin
  if Depth > 64 then Fail('Storage configuration is too deeply nested.');
  SkipSpace(Text, Position);
  if Position > Length(Text) then Fail('Incomplete storage configuration.');
  C := Text[Position];
  if C = '"' then begin
    Value := JsonString(Text, Position);
    StringChangeEx(Value, '/', '\', True);
    if CheckPaths and (((Length(Value) > 2) and (Copy(Value, 2, 2) = ':\')) or (Copy(Value, 1, 2) = '\\')) then
      if Overlaps(Value, ExpandConstant('{app}')) then Fail('Move the conflicting storage location or cancel its pending migration first: ' + Value);
  end else if (C = '{') or (C = '[') then begin
    Ending := ']'; if C = '{' then Ending := '}';
    Position := Position + 1; SkipSpace(Text, Position);
    if (Position <= Length(Text)) and (Text[Position] = Ending) then begin Position := Position + 1; Exit; end;
    repeat
      Nested := CheckPaths;
      if C = '{' then begin
        SkipSpace(Text, Position);
        if Position > Length(Text) then Fail('Incomplete storage configuration.');
        Value := JsonString(Text, Position);
        SkipSpace(Text, Position);
        if (Position > Length(Text)) or (Text[Position] <> ':') then Fail('Invalid storage configuration.');
        Position := Position + 1;
        if (Depth = 0) and (Value = 'results') then Nested := False;
      end;
      CheckJsonValue(Text, Position, Depth + 1, Nested); SkipSpace(Text, Position);
      if Position > Length(Text) then Fail('Incomplete storage configuration.');
      if Text[Position] = Ending then begin Position := Position + 1; Exit; end;
      if Text[Position] <> ',' then Fail('Invalid storage configuration.');
      Position := Position + 1;
    until False;
  end else begin
    Start := Position;
    while (Position <= Length(Text)) and (Pos(Text[Position], ',}] ' + #9 + #10 + #13) = 0) do Position := Position + 1;
    Value := Copy(Text, Start, Position - Start);
    if (Value <> 'true') and (Value <> 'false') and (Value <> 'null') then begin
      if Value = '' then Fail('Invalid storage configuration.');
      for Start := 1 to Length(Value) do
        if Pos(Value[Start], '0123456789.eE+-') = 0 then Fail('Invalid storage configuration.');
    end;
  end;
end;

procedure CheckStorageFile(Name: String);
var Lines: TArrayOfString; Text: String; I, Position: Integer;
begin
  CheckPlain(Name);
  if not FileExists(Name) then Exit;
  if not LoadStringsFromFile(Name, Lines) then Fail('Cannot read storage configuration.');
  Text := '';
  for I := 0 to GetArrayLength(Lines) - 1 do Text := Text + Lines[I] + #10;
  Position := 1; CheckJsonValue(Text, Position, 0, True); SkipSpace(Text, Position);
  if Position <= Length(Text) then Fail('Invalid storage configuration.');
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
    if not MatchesLines(Temporary, Lines) then Fail('Move the conflicting metadata file before retrying: ' + Temporary);
  end else if not SaveStringsToUTF8File(Temporary, Lines, False) then Fail('Cannot save installation metadata.');
  if not MoveFileEx(Temporary, Name, 9) then Fail('Cannot publish installation metadata: ' + Name);
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfoInfo, MemoTypeInfoInfo, MemoComponentsInfoInfo, MemoGroupInfoInfo, MemoTasksInfoInfo: String): String;
begin
  ReadIdentity;
  if RegisteredVersion = '' then Result := 'New installation: {#AppVersion}'
  else if CompareVersions(RegisteredVersion, '{#AppVersion}') = 0 then Result := 'Repair installation: {#AppVersion}'
  else Result := RegisteredVersion + ' -> {#AppVersion}';
  Result := Result + NewLine + MemoDirInfoInfo + NewLine + MemoTasksInfoInfo;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Version: String;
begin
  Result := ''; Prepared := False;
  try
    CheckApplication;
    ReadIdentity;
    CheckPlain(ExpandConstant('{app}'));
    CheckStorage;
    ExtractTemporaryFile(ManifestName);
    NewManifestHash := GetSHA256OfFile(ExpandConstant('{tmp}\') + ManifestName);
    if not LoadStringsFromFile(ExpandConstant('{tmp}\') + ManifestName, NewLines) then Fail('Cannot read the new installation manifest.');
    ReadManifest(NewLines, NewFiles, Version);
    if Version <> '{#AppVersion}' then Fail('The installer and program versions do not match.');
    ReadBaseline;
    VerificationCancelled := False; Verifying := True;
    VerificationPage.Show;
    try CheckTree(''); finally Verifying := False; VerificationPage.Hide; end;
    if (RegisteredVersion <> '') and (CompareVersions(RegisteredVersion, '{#AppVersion}') > 0) then begin
      if WizardSilent then Fail('Silent downgrades are not supported.');
      if ConfirmedDowngrade <> RegisteredVersion then begin
        if MsgBox('Installed: ' + RegisteredVersion + #13#10 + 'Continue with older version {#AppVersion}?' + #13#10 +
            'Back up your data first. Newer projects and data may not open in the older version.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then
          Fail('Downgrade cancelled. No program files have been changed.');
        ConfirmedDowngrade := RegisteredVersion;
      end;
    end;
    PreflightVersion := RegisteredVersion;
    Prepared := True;
  except Result := GetExceptionMessage; end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var I, Index: Integer; Name: String;
begin
  if CurStep = ssInstall then begin
    if not Prepared then Fail('Installation preflight has not completed.');
    CheckApplication; ReadIdentity;
    if RegisteredVersion <> PreflightVersion then Fail('The installed version has changed. Restart this installer.');
    if LegacyRegistrationOnly then CheckLegacyDirectory;
    CheckStorage; CheckTree('');
    if not ForceDirectories(ExpandConstant('{app}')) then Fail('Cannot create the program directory.');
    if not Repairing then begin
      AtomicLines(ExpandConstant('{app}\') + PendingName, RecoveryLines);
      Repairing := True;
    end;
  end;
  if CurStep = ssPostInstall then begin
    for I := 0 to GetArrayLength(NewFiles) - 1 do begin
      Name := ExpandConstant('{app}\') + NewFiles[I].Name; CheckPlain(Name);
      if not FileExists(Name) or (CompareText(GetSHA256OfFile(Name), NewFiles[I].Hash) <> 0) then
        Fail('Program verification failed. Run this installer again to repair: ' + NewFiles[I].Name);
    end;
    for I := 0 to GetArrayLength(OldFiles) - 1 do begin
      Index := FindProgramFile(NewFiles, OldFiles[I].Name);
      Name := ExpandConstant('{app}\') + OldFiles[I].Name;
      if (Index < 0) and FileExists(Name) then begin
        CheckPlain(Name);
        if CompareText(GetSHA256OfFile(Name), OldFiles[I].Hash) <> 0 then Fail('Old program file has changed: ' + Name);
        if not DeleteFile(Name) then Fail('Cannot remove the old program file. Run this installer again: ' + Name);
        Name := ExtractFileDir(Name);
        while NormalPath(Name) <> NormalPath(ExpandConstant('{app}')) do begin
          if not RemoveDir(Name) then Break;
          Name := ExtractFileDir(Name);
        end;
      end;
    end;
    AtomicLines(ExpandConstant('{app}\') + ManifestName, NewLines);
    if not DeleteFile(ExpandConstant('{app}\') + PendingName) then Fail('Cannot finish installation. Run this installer again.');
    Completed := True;
  end;
end;

procedure InitializeWizard;
begin
  VerificationPage := CreateOutputProgressPage('Checking installation', 'Existing files are being verified.');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and Completed then
    WizardForm.FinishedLabel.Caption := 'Installation is complete. Close this installer before starting OpenPictureStudio.';
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
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := False;
  try
    MaintenanceHandle := SharedMutex(MaintenanceMarker, True);
    if FileExists(ExpandConstant('{app}\') + PendingName) then Fail('Repair the incomplete installation before removing it.');
    CheckApplication;
    Result := True;
  except
    MsgBox(GetExceptionMessage, mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Version: String;
begin
  if CurUninstallStep = usUninstall then begin
    CheckApplication; CheckStorage;
    ReadIdentity;
    if not LoadStringsFromFile(ExpandConstant('{app}\') + ManifestName, OldLines) then Fail('Cannot verify installed files. Repair the installation first.');
    ReadManifest(OldLines, OldFiles, Version);
    SetArrayLength(NewFiles, 0); Repairing := False;
    CheckTree('');
  end;
end;

procedure DeinitializeUninstall;
begin
  if MaintenanceHandle <> 0 then CloseHandle(MaintenanceHandle);
end;
