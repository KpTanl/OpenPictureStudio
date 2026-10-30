# Installation and Upgrades

[简体中文](installation.md) | [English](installation.en.md) | [Français](installation.fr.md)

[Back to README](../README.en.md)

### Download the Windows Installer

1. Open the [latest release](https://github.com/KpTanl/OpenPictureStudio/releases/latest). Under **Assets**, download `OpenPictureStudio-<version>-beta-setup.exe` and the `.exe.sha256` file with the same name.
2. Verify the installer's SHA256, then double-click the EXE and follow the setup wizard.
3. Start OpenPictureStudio and open a photo or folder. The installed version needs no separate Python or Rust. AI models are downloaded as needed.

<details>
<summary>How do I verify the SHA256?</summary>

Open PowerShell in the download folder and run the commands below. Replace `<version>` in the file names with the actual version:

```powershell
Get-FileHash -LiteralPath '.\OpenPictureStudio-<version>-beta-setup.exe' -Algorithm SHA256
Get-Content -LiteralPath '.\OpenPictureStudio-<version>-beta-setup.exe.sha256'
```

The hash shown by the first command should match the 64-character value at the start of the second command's output. The checksum confirms that the downloaded file is complete and belongs to the same release asset. Get the installer and the checksum file from this repository's Release.

</details>

### "Windows protected your PC" on First Run

The current Windows installer is not code-signed. The first time you double-click it, Windows SmartScreen may show "Windows protected your PC" and "Unknown publisher".

Make sure the file comes from [this repository's Release](https://github.com/KpTanl/OpenPictureStudio/releases/latest) and check the SHA256 for the same version. Then click **"More info" → "Run anyway"** to continue the installation. The example screenshot below has "More info" expanded:

<details>
<summary>View the Windows first-run prompt</summary>

<img src="../.github/assets/readme/windows-smartscreen.zh-CN.png" width="480" alt="Windows SmartScreen prompt (Chinese interface), with Run anyway and Don't run buttons at the bottom">

The example installer is version 0.16.0. The file name changes with each release. If your device does not offer "Run anyway", follow the system prompts or your organization's policy.

</details>

### Download and Install with PowerShell

Open a regular PowerShell window, expand the command below, and **copy and run the whole block**. The command works with Windows PowerShell 5.1 and PowerShell 7. It downloads the latest stable release from this repository, verifies the SHA256, and opens the setup wizard.

Before upgrading, quit OPS normally. The downloaded files stay in the temporary folder that the command prints, and the installation does not restart your computer automatically. Windows protection and organization policies still apply as usual.

<details>
<summary>Show the PowerShell install command</summary>

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $releasePage = 'https://github.com/KpTanl/OpenPictureStudio/releases/latest'
    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/KpTanl/OpenPictureStudio/releases/latest' -Headers @{ 'User-Agent' = 'OpenPictureStudio-Install' } -TimeoutSec 30
        $name = 'OpenPictureStudio-{0}-beta-setup.exe' -f ($release.tag_name -replace '^v', '')
        $installer = @($release.assets | Where-Object { $_.name -ceq $name })
        $checksum = @($release.assets | Where-Object { $_.name -ceq "$name.sha256" })
        if ($installer.Count -ne 1 -or $checksum.Count -ne 1) { throw 'The release does not contain exactly one matching installer and checksum file.' }

        $folder = Join-Path ([IO.Path]::GetTempPath()) ('OpenPictureStudio-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $folder | Out-Null
        Write-Host ('Download folder: ' + $folder)
        $exe = Join-Path $folder $name
        $sha = Join-Path $folder "$name.sha256"
        Invoke-WebRequest -UseBasicParsing -Uri $installer[0].browser_download_url -OutFile $exe -TimeoutSec 600
        Invoke-WebRequest -UseBasicParsing -Uri $checksum[0].browser_download_url -OutFile $sha -TimeoutSec 30

        $line = (Get-Content -LiteralPath $sha -Raw).Trim()
        if ($line -notmatch '^(?<hash>[a-fA-F0-9]{64})[ \t]+\*?(?<file>[^\r\n]+)$') { throw 'The checksum file is malformed, or it belongs to a different installer.' }
        $expected = $Matches['hash']
        if ($Matches['file'] -cne $name) { throw 'The checksum file is malformed, or it belongs to a different installer.' }
        if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ine $expected) { throw 'SHA256 does not match. The installer was not started.' }

        Write-Host 'SHA256 verified. Opening the setup wizard...'
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $exe
        $start.WorkingDirectory = $folder
        $start.UseShellExecute = $false
        $start.Arguments = '/NORESTART'
        $process = [System.Diagnostics.Process]::Start($start)
        $process.WaitForExit()
        switch ($process.ExitCode) {
            0 { Write-Host 'Installation complete.' }
            { $_ -in 2, 5 } { Write-Host 'Installation was canceled or aborted.' }
            default { throw ('Installation did not finish. Exit code: ' + $process.ExitCode) }
        }
    } catch {
        throw ("Could not complete the installation. Release page: $releasePage`n" + $_.Exception.Message)
    }
}
```

</details>

### Upgrades and Version Checks

By default, the installed version checks for a stable release automatically, at most once every 24 hours. You can turn this off in Settings → General, or check manually on the About page. The app opens the download page, and you download and install the update yourself. The check includes the app version and the necessary HTTP information, and GitHub can see the IP address and request time.

<details>
<summary>Upgrading in place, repair, and early test builds</summary>

Before upgrading, quit every running OpenPictureStudio, then run the new installer over the existing installation. When the installation finishes, close the installer and restart the app. Your existing settings, custom data folder, models, and projects are kept.

If there is a folder conflict, follow the installer's prompts to back up and move out the conflicting files. If an installation was interrupted or you need a repair, run the full installer of the same version again and follow the prompts to complete the repair.

Early test builds may not have an install manifest. If the original program is still there, back up your data and uninstall the old version first. If the original install folder has been deleted or is completely empty, you can simply install again. When the installer blocks the overwrite, it shows the original install folder.

</details>

## UI Components and Licensing

Starting with 0.17.0, the UI components moved from PyQt6 to PySide6. Versions that use PySide6 ship the components they need in the full installer, so upgrade in place with the steps above, with no need to install PySide6, Shiboken, or Qt separately.

PyQt6 is offered under GPLv3 and a commercial license. PySide6 is offered under open-source licenses such as LGPLv3, with commercial license options. After the move, the project's original code stays under the MIT License, and the UI components, other dependencies, and models follow their own licenses. License notices and corresponding source information ship with the program. See [Licensing and Third-Party Components](licensing.en.md#migrating-from-pyqt6-to-pyside6) for details.
