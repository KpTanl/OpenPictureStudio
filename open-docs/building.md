# 开发与构建

[简体中文](building.md) | [English](building.en.md) | [Français](building.fr.md)

[返回首页](../README.md) · [文档目录](README.md)

本指南介绍如何从源码运行 OpenPictureStudio、构建 Rust 原生加速扩展与 Windows 安装包。

## 环境准备与源码运行

开发与发行构建使用 **Python 3.13**（最低支持 Python 3.12）。依赖按能力分层，可按需安装：

```powershell
# 1. 获取源码并创建虚拟环境
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. 安装基础运行依赖（看图、调色、格式处理与导出）
pip install -r requirements.txt

# 3.（可选）安装本地 AI 与人像美颜依赖
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. 启动应用
python main.py
```

源码运行时若未构建原生模块，相关图像计算会自动回退至 Python／NumPy 路径。测试运行方式见 [tests/README.md](../tests/README.md)。

## 构建原生加速模块

`ops_accel.pyd` 为可选原生模块（完整发行构建必需）。编译需要 **Rust 工具链**、**MSVC C++ Build Tools** 与 **Windows SDK**；默认启用的静态 DXC 着色器编译器还要求安装 **“C++ ATL for x64/x86”** 组件（首次编译会自动下载并校验固定版本的 DXC 静态库）：

```powershell
cd native\ops_accel
cargo build --release --locked
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

| 构建变体 | 用途说明 |
| --- | --- |
| 指定 `OPS_ATL_LIB` 环境变量 | 指向本地已备妥 `atls.lib` 的目录 |
| `cargo build --release --locked --no-default-features` | 构建仅包含 FXC 编译器的原生模块（不依赖 ATL） |

发行构建保持通用 x86_64 目标，AVX2 在运行时自动检测启用；需要排查或对照无 AVX2 环境时，可使用 `OPS_CPU_KERNEL=portable`（详见[CPU 计算与兼容性](architecture.md#cpu-计算与兼容性)）。

## 构建 Windows 安装包

打包需要 **Inno Setup 6.7.1**、Rust 工具链以及锁定构建依赖（见 [requirements-build.lock.txt](../requirements-build.lock.txt)，包含 `PySide6_Essentials==6.11.3` 与 `shiboken6==6.11.3`，并排除 PyQt5／PyQt6／PySide2）：

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

> **注意**：`build.ps1` 固定调用仓库根目录 `.venv\Scripts\python.exe`，要求所有发行源码已提交且工作区干净，并在开始时清理 `build/` 与 `dist/` 目录。执行许可证收集前需确保本地 Cargo 依赖源码已就位（可执行 `cargo fetch --locked`）。

- **产物输出**：完整程序目录位于 `dist/OpenPictureStudio/`，安装包及同名 `.exe.sha256` 位于 `dist/installer/`。
- **跳过原生重编**：确认原生扩展已匹配当前源码时，可附加 `-SkipNativeBuild` 复用根目录 `ops_accel.pyd`。

## CI 与自动构建

仓库通过 GitHub Actions 自动执行代码检查、原生模块编译与完整测试；推送版本标签时，自动构建 Windows 正式安装包并生成 `.sha256` 校验文件，所有发行资产均由公开工作流直接产出。

## 相关文档

- [架构与原生加速](architecture.md)
- [图像插件接口](image-plugins.md)
- [许可证与第三方组件](licensing.md)
