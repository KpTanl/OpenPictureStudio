# 开发与构建

[简体中文](building.md) | [English](building.en.md) | [Français](building.fr.md)

[返回首页](../README.md)

## 源码运行

需要 Python 3.12 及以上（开发与发行构建使用 3.13）。依赖按功能分层，可以只装需要的部分。

```powershell
# 1. 获取源码
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. 安装基础依赖（看图、调色、格式处理与输出）
pip install -r requirements.txt

# 3.（可选）安装 AI 与美颜依赖
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. 启动应用
python main.py
```

Rust 加速模块是可选的。需要时安装 Rust 工具链后构建，并把产物复制到仓库根目录：

```powershell
cd native\ops_accel
cargo build --release --locked
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

## 构建

### 构建安装包

需要 Inno Setup、Rust 工具链，以及完整依赖（程序、AI、美颜与打包）。`requirements-build.lock.txt` 锁定了经过测试的版本，发行构建用它安装；`requirements-build.txt` 是它的来源。构建前会检查 Python 版本、依赖是否齐全，以及环境中只有一个 OpenCV 分发。

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

### CI 与发布

CI 以独立 job 依次执行代码检查与 Python 预编译、原生模块构建、测试、安装包构建。PR 执行到测试；`dev`／`main` 推送和手动运行继续打包。

版本标签执行相同阶段，核对提交已进入 `main`、标签与 `APP_VERSION` 一致，再创建 Release 草稿。维护者手动完成最终发布。

原生构建上传 `ops_accel.pyd`，测试和打包下载同一份产物；打包通过 `build.ps1 -SkipNativeBuild` 复用该模块，并准备 Cargo 依赖源码供许可收集。下游 job 按 producer 的 artifact ID 选择产物。

### 运行测试

测试说明见 [tests/README.md](../tests/README.md)。

## PySide6 与原生模块

基础依赖锁定 `PySide6_Essentials==6.11.3` 和 `shiboken6==6.11.3`。发行依赖以 [requirements-build.lock.txt](../requirements-build.lock.txt) 为准，Qt 绑定使用 PySide6。`build.ps1` 固定使用仓库根目录的 `.venv\Scripts\python.exe`，并要求所有发行源码已提交且工作区干净。普通源码运行可以在开发工作区进行。

AVX2 在运行时检测；发行构建保持通用 CPU 目标。原生模块缺失时，源码运行的相关计算可使用 Python／NumPy 路径；完整发行构建要求成功生成或显式复用 `ops_accel.pyd`。

构建脚本会清理 `build/` 和 `dist/`，先保存其中需要保留的产物。`-SkipNativeBuild` 仅适用于已确认与候选源码匹配的原生模块。许可收集需要本地 Cargo 依赖源码，可先执行 `cargo fetch --locked`。

产物：`dist/OpenPictureStudio/` 程序目录，以及 `dist/installer/` 中的 EXE 和 `.exe.sha256`。发布前完成下方手动验收，并核对程序目录中的原生模块与本次构建一致。

## PySide6 迁移与发行验收

当前源码和打包配置采用 PySide6 Essentials 与 Shiboken 6.11.3，并排除 PyQt5、PyQt6 和 PySide2。发行前手动核对以下项目：

1. 在测试账户安装上一版，记录版本、安装目录及资料目录；备份设置、AI 模型、`.opsproj` 项目和自定义资料。
2. 关闭全部 OPS 实例，在同一目录覆盖安装候选版；检查程序能够启动、旧版 PyQt 文件按安装清单移除、PySide6 与 Shiboken 可正常加载。
3. 打开原有项目，核对编辑历史、模型引用与导出；检查设置和自定义资料目录保持可用。
4. 在设置中确认模型与运行环境仍被识别，分别打开看图、RAW、蒙版、美颜和助手界面。
5. 取消一次测试安装，再运行同一版本完整安装包；按安装器提示处理备份与冲突，核对修复后可以启动。
6. 在有 AVX2 的机器和无 AVX2 的机器分别验证启动、调色和导出；`portable` 可验证通用内核，实际硬件兼容性记录需来自对应机器。

验收时记录安装包版本、SHA256、Windows 版本、资料目录和结果。
