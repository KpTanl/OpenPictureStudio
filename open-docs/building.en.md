# Development and Building

[简体中文](building.md) | [English](building.en.md) | [Français](building.fr.md)

[Back to Home](../README.en.md) · [Documentation](README.en.md)

This guide covers running OpenPictureStudio from source, compiling the Rust native acceleration module, and building the Windows installer.

## Prerequisites and Running from Source

Development and release builds target **Python 3.13** (minimum Python 3.12). Dependencies are layered so you can install only what you need:

```powershell
# 1. Clone the repository and create a virtual environment
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. Install base dependencies (viewing, color grading, formats, export)
pip install -r requirements.txt

# 3. (Optional) Install local AI and portrait beauty dependencies
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. Launch the application
python main.py
```

When running from source without the compiled native module, image processing automatically falls back to Python/NumPy implementations. See [tests/README.md](../tests/README.md) for running tests.

## Building the Native Acceleration Module

`ops_accel.pyd` is optional when running from source and required for release installers. Building requires the **Rust 1.95.0 toolchain** (pinned by `native/ops_accel/rust-toolchain.toml` and matched automatically by `rustup`), **MSVC C++ Build Tools**, and the **Windows SDK**. Default static DXC shader compilation also requires the **"C++ ATL for x64/x86"** component (the first build downloads and verifies a pinned DXC static archive):

```powershell
cd native\ops_accel
cargo build --release --locked
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

| Build Variant | Purpose |
| --- | --- |
| Set `OPS_ATL_LIB` env var | Point directly to a folder containing `atls.lib` |
| `cargo build --release --locked --no-default-features` | Build FXC-only native module without ATL |

Release builds keep a portable x86_64 CPU target and detect AVX2 at runtime. To test or compare non-AVX2 behavior, launch with `OPS_CPU_KERNEL=portable` (see [CPU Computation and Compatibility](architecture.en.md#cpu-computation-and-compatibility)).

## Building the Windows Installer

Packaging requires **Inno Setup 6.7.1**, the Rust toolchain, and pinned build dependencies from [requirements-build.lock.txt](../requirements-build.lock.txt) (which locks `PySide6_Essentials==6.11.3` and `shiboken6==6.11.3` and excludes PyQt5/PyQt6/PySide2):

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

> **Note:** `build.ps1` uses `.venv\Scripts\python.exe`, requires a clean Git working tree with all release sources committed, and wipes `build/` and `dist/` at start. Make sure local Cargo dependency sources are cached (`cargo fetch --locked`) before license collection runs.

- **Build outputs:** The staged application folder is written to `dist/OpenPictureStudio/`, and the installer EXE plus `.exe.sha256` checksum are written to `dist/installer/`.
- **Reusing native module:** Pass `-SkipNativeBuild` if `ops_accel.pyd` in the repository root is already compiled from the matching source.

## CI and Automated Releases

GitHub Actions runs Python (`ruff`, `compileall`, `pytest`) and Rust (`clippy`, `cargo test`) checks alongside native module builds in parallel. Pushing a release tag builds the Windows installer and matching `.sha256` checksum assets from the public workflow and creates a Draft Release awaiting confirmation.

## Related Documentation

- [Architecture and Native Acceleration](architecture.en.md)
- [Image Plugins](image-plugins.en.md)
- [Licensing and Third-Party Components](licensing.en.md)
