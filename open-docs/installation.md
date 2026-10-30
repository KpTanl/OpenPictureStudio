# 安装与升级

[简体中文](installation.md) | [English](installation.en.md) | [Français](installation.fr.md)

[返回首页](../README.md)

### 下载 Windows 安装包

1. 打开 [最新版本](https://github.com/KpTanl/OpenPictureStudio/releases/latest)，在 **Assets** 中下载 `OpenPictureStudio-<版本>-beta-setup.exe` 和同名 `.exe.sha256` 文件。
2. 核对安装包的 SHA256，双击 EXE，按安装向导完成安装。
3. 启动 OpenPictureStudio，打开照片或文件夹。安装版无需另装 Python 或 Rust；AI 模型按需下载。

<details>
<summary>如何核对 SHA256？</summary>

在下载目录打开 PowerShell，运行下面的命令，将文件名中的 `<版本>` 换成实际版本：

```powershell
Get-FileHash -LiteralPath '.\OpenPictureStudio-<版本>-beta-setup.exe' -Algorithm SHA256
Get-Content -LiteralPath '.\OpenPictureStudio-<版本>-beta-setup.exe.sha256'
```

第一条命令显示的哈希应与第二条输出开头的 64 位值一致。校验值用于核对下载文件是否完整，并对应同一份发行资产。请从本仓库的 Release 获取安装包与校验文件。

</details>

### 首次运行出现“Windows 已保护你的电脑”

当前 Windows 安装包未作代码签名。首次双击时，Windows SmartScreen 可能显示“Windows 已保护你的电脑”及“发布者未知”。

确认文件来自 [本仓库的 Release](https://github.com/KpTanl/OpenPictureStudio/releases/latest)，并核对同版本 SHA256 后，点击 **“更多信息” → “仍要运行”**，继续安装。下面的示例截图已展开“更多信息”：

<details>
<summary>查看 Windows 首次运行提示</summary>

<img src="../.github/assets/readme/windows-smartscreen.zh-CN.png" width="480" alt="Windows SmartScreen 提示，底部有仍要运行和不运行按钮">

示例安装包版本为 0.16.0；文件名随发行版本变化。设备没有提供“仍要运行”时，请按系统提示或组织策略处理。

</details>

### 使用 PowerShell 下载安装

打开普通 PowerShell，展开下方命令并**整段复制执行**。命令兼容 Windows PowerShell 5.1 与 PowerShell 7，从本仓库下载最新正式发行包，校验 SHA256 后直接打开安装向导。

升级前请正常退出 OPS。下载文件保留在命令显示的临时目录，安装过程不会自动重启电脑。Windows 防护和组织策略照常生效。

<details>
<summary>展开 PowerShell 安装命令</summary>

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $releasePage = 'https://github.com/KpTanl/OpenPictureStudio/releases/latest'
    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/KpTanl/OpenPictureStudio/releases/latest' -Headers @{ 'User-Agent' = 'OpenPictureStudio-Install' } -TimeoutSec 30
        $name = 'OpenPictureStudio-{0}-beta-setup.exe' -f ($release.tag_name -replace '^v', '')
        $installer = @($release.assets | Where-Object { $_.name -ceq $name })
        $checksum = @($release.assets | Where-Object { $_.name -ceq "$name.sha256" })
        if ($installer.Count -ne 1 -or $checksum.Count -ne 1) { throw '发行版本缺少唯一匹配的安装包或校验文件。' }

        $folder = Join-Path ([IO.Path]::GetTempPath()) ('OpenPictureStudio-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $folder | Out-Null
        Write-Host ('下载目录：' + $folder)
        $exe = Join-Path $folder $name
        $sha = Join-Path $folder "$name.sha256"
        Invoke-WebRequest -UseBasicParsing -Uri $installer[0].browser_download_url -OutFile $exe -TimeoutSec 600
        Invoke-WebRequest -UseBasicParsing -Uri $checksum[0].browser_download_url -OutFile $sha -TimeoutSec 30

        $line = (Get-Content -LiteralPath $sha -Raw).Trim()
        if ($line -notmatch '^(?<hash>[a-fA-F0-9]{64})[ \t]+\*?(?<file>[^\r\n]+)$') { throw '校验文件格式不正确，或对应另一个安装包。' }
        $expected = $Matches['hash']
        if ($Matches['file'] -cne $name) { throw '校验文件格式不正确，或对应另一个安装包。' }
        if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ine $expected) { throw 'SHA256 不匹配，未启动安装器。' }

        Write-Host 'SHA256 已核对，正在打开安装向导……'
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $exe
        $start.WorkingDirectory = $folder
        $start.UseShellExecute = $false
        $start.Arguments = '/NORESTART'
        $process = [System.Diagnostics.Process]::Start($start)
        $process.WaitForExit()
        switch ($process.ExitCode) {
            0 { Write-Host '安装完成。' }
            { $_ -in 2, 5 } { Write-Host '安装已取消或中止。' }
            default { throw ('安装未完成，退出码：' + $process.ExitCode) }
        }
    } catch {
        throw ("未能完成安装，发行页面：$releasePage`n" + $_.Exception.Message)
    }
}
```

</details>

### 升级与版本检查

安装版默认最多每 24 小时自动检查一次正式版，可在“设置 → 常规”关闭，也可在“关于”页手动检查。程序打开下载页，由你下载安装。查询包含应用版本及必要 HTTP 信息，GitHub 可见 IP 和请求时间。

<details>
<summary>覆盖升级、修复与早期测试版</summary>

升级前，请先退出所有正在运行的 OpenPictureStudio，再运行新版安装包覆盖升级。安装完成后，关闭安装器并重新启动程序。现有设置、自定义资料目录、模型和项目会保留。

遇到目录冲突时，请按安装器提示备份并移出冲突文件。安装中断或需要修复时，重新运行同一版本的完整安装包，按提示完成修复。

早期测试版可能没有安装清单。原程序仍在时，先备份资料并卸载旧版；原安装目录已删除或完全为空时，可直接重新安装。安装器阻止覆盖时会显示原安装目录。

</details>

## 界面组件与许可

自 0.17.0 起，界面组件由 PyQt6 迁移到 PySide6。采用 PySide6 的版本通过完整安装包提供所需组件，按上述步骤覆盖升级即可，无需单独安装 PySide6、Shiboken 或 Qt。

PyQt6 提供 GPLv3 和商业许可；PySide6 提供 LGPLv3 等开源许可及商业许可选项。迁移后，项目原创代码继续采用 MIT 许可，界面组件、其他依赖和模型遵循各自的许可。许可声明与对应源码信息随程序提供，详见[许可证与第三方组件](licensing.md#从-pyqt6-迁移到-pyside6)。
