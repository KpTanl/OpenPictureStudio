<#
    build.ps1 - one-shot packaging for the OpenPictureStudio beta installer.

    Pipeline: clean -> PyInstaller (full AI, windowed) -> third-party licenses
              -> installation manifest -> Inno Setup -> setup.exe

    The bundle includes the full AI stack (rembg / onnxruntime / mediapipe).
    AI *model files* are not bundled; the app downloads them to %APPDATA% on
    first use. Stage 1 estimates: a ~505 MB program folder and a ~138 MB
    installer. Actual sizes are reported after the build.

    Usage (from repo root):
        powershell -ExecutionPolicy Bypass -File build.ps1

    Output:
        dist\OpenPictureStudio\                                  portable program folder
        dist\installer\OpenPictureStudio-<ver>-beta-setup.exe    installer to hand to testers

    NOTE: kept ASCII-only on purpose. Windows PowerShell 5.1 decodes BOM-less
    scripts with the system ANSI codepage, so non-ASCII text here breaks parsing.
#>

param([switch]$SkipNativeBuild)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# --- 1. Locate venv Python -------------------------------------------------
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "venv not found: $Python  (create .venv then: pip install -r requirements-dev.txt)"
}

# --- 2. Read version from src/utils/app_info.py ---------------------------
$AppInfo = Join-Path $RepoRoot "src\utils\app_info.py"
$VersionMatch = Select-String -Path $AppInfo -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
if (-not $VersionMatch) { throw "Could not parse APP_VERSION from $AppInfo" }
$AppVersion = $VersionMatch.Matches[0].Groups[1].Value
Write-Step "Building version: $AppVersion (full AI beta)"
if ($AppVersion -notmatch '^\d{1,9}\.\d{1,9}\.\d{1,9}$') { throw "Invalid version: $AppVersion" }
$SourceSha = & git rev-parse --verify HEAD
if ($LASTEXITCODE -ne 0) { throw "Cannot read source commit" }
$CandidateChanges = & git status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $CandidateChanges) { throw "Release builds require a clean, committed source candidate" }

function Assert-SourceCandidate {
    $CurrentSourceSha = & git rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or $CurrentSourceSha -ne $SourceSha) { throw "Source commit changed during the build" }
    $CurrentSourceChanges = & git status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0 -or $CurrentSourceChanges) { throw "Source files changed during the build" }
}

# --- 3. Verify the full build environment ---------------------------------
& $Python -B tools\check_build_env.py
if ($LASTEXITCODE -ne 0) {
    throw "Build environment incomplete. Run: .venv\Scripts\python.exe -m pip install -r requirements-build.lock.txt"
}

# --- 4. Locate Inno Setup ISCC.exe ----------------------------------------
$IsccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup (ISCC.exe) not found. Install: winget install JRSoftware.InnoSetup"
}
Write-Host "Inno Setup: $Iscc"

# --- 5. Clean previous output ---------------------------------------------
Write-Step "Cleaning build\ and dist\"
foreach ($dir in @("build", "dist")) {
    $p = [IO.Path]::GetFullPath((Join-Path $RepoRoot $dir))
    if ((Split-Path -Path $p -Parent) -ne [IO.Path]::GetFullPath($RepoRoot)) { throw "Unsafe output path: $p" }
    if (Test-Path -LiteralPath $p) {
        $OutputItem = Get-Item -LiteralPath $p -Force
        if ($OutputItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked output directory: $p" }
        $LinkedOutput = Get-ChildItem -LiteralPath $p -Recurse -Force | Where-Object {
            $_.Attributes -band [IO.FileAttributes]::ReparsePoint
        } | Select-Object -First 1
        if ($LinkedOutput) { throw "Linked output entry: $($LinkedOutput.FullName)" }
        Remove-Item -LiteralPath $p -Recurse -Force
    }
}

# --- 5.1 Build native Rust acceleration extension (ops_accel) --------------
$CargoToml = Join-Path $RepoRoot "native\ops_accel\Cargo.toml"
if ($SkipNativeBuild) {
    $PydPath = Join-Path $RepoRoot "ops_accel.pyd"
    if (-not (Test-Path -LiteralPath $PydPath)) { throw "Native extension not found: $PydPath" }
} elseif (Test-Path $CargoToml) {
    Write-Step "Building native Rust acceleration module (ops_accel)..."
    Push-Location (Join-Path $RepoRoot "native\ops_accel")
    try {
        & cargo build --release --locked
        if ($LASTEXITCODE -eq 0) {
            $DllPath = Join-Path $RepoRoot "native\ops_accel\target\release\ops_accel.dll"
            $PydPath = Join-Path $RepoRoot "ops_accel.pyd"
            if (Test-Path $DllPath) {
                Copy-Item -Force $DllPath $PydPath
                Write-Host "Built and copied native module to: $PydPath" -ForegroundColor Green
            }
            if (-not (Test-Path -LiteralPath $DllPath)) { throw "Native extension was not produced" }
        } else {
            throw "Native build failed"
        }
    } finally {
        Pop-Location
    }
}

# --- 6. PyInstaller --------------------------------------------------------
Write-Step "Running PyInstaller (first run is slow)..."
& $Python -m PyInstaller --noconfirm --clean OpenPictureStudio.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }


$ProgramDir = Join-Path $RepoRoot "dist\OpenPictureStudio"
$ExePath = Join-Path $ProgramDir "OpenPictureStudio.exe"
if (-not (Test-Path $ExePath)) { throw "Expected exe not produced: $ExePath" }
$ProgramFiles = @(Get-ChildItem -LiteralPath $ProgramDir -Recurse -File -Force)
$ExcludedFiles = @($ProgramFiles | Where-Object {
    $_.Name -like "opencv_videoio_ffmpeg*.dll" -or $_.Name -in @("opengl32sw.dll", "Qt6Pdf.dll", "qpdf.dll")
})
if ($ExcludedFiles.Count -gt 0) {
    throw "Excluded native libraries found in program folder: $($ExcludedFiles.FullName -join ', ')"
}
$ProgramSizeMB = [math]::Round(($ProgramFiles | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "Program folder: dist\OpenPictureStudio\"
Write-Host "Program size:   $ProgramSizeMB MiB"

# --- 7. Third-party licenses and corresponding source -----------------------
Write-Step "Collecting third-party licenses..."
Assert-SourceCandidate
& $Python -B tools\collect_licenses.py $ProgramDir "--version=$AppVersion" "--source-sha=$SourceSha"
if ($LASTEXITCODE -ne 0) { throw "License collection failed; see the problems listed above" }

# --- 8. Inno Setup ---------------------------------------------------------
Write-Step "Writing installation file manifest..."
Assert-SourceCandidate
& $Python -B tools\install_manifest.py $ProgramDir "--version=$AppVersion" "--source-sha=$SourceSha"
if ($LASTEXITCODE -ne 0) { throw "Installation manifest failed" }
Write-Step "Running Inno Setup..."
& $Iscc "/DAppVersion=$AppVersion" "/DRepoRoot=$RepoRoot" (Join-Path $RepoRoot "packaging\OpenPictureStudio.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }
Assert-SourceCandidate

$Setup = Join-Path $RepoRoot "dist\installer\OpenPictureStudio-$AppVersion-beta-setup.exe"
if (-not (Test-Path $Setup)) { throw "Installer not produced: $Setup" }

$SizeMB = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
$InstallerHash = (Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$Setup.sha256", "$InstallerHash  $([IO.Path]::GetFileName($Setup))`n", [Text.UTF8Encoding]::new($false))
Write-Step "Done"
Write-Host "Installer: $Setup" -ForegroundColor Green
Write-Host "Size:      $SizeMB MiB" -ForegroundColor Green
Write-Host "SHA256:    $InstallerHash"
Write-Host "Hand this setup.exe to your beta testers." -ForegroundColor Green
