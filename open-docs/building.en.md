# Development and Building

[简体中文](building.md) | [English](building.en.md) | [Français](building.fr.md)

[Back to README](../README.en.md)

## Running from Source

Python 3.12 or later is required (development and release builds use 3.13). Dependencies are split by feature, so you can install only the parts you need.

```powershell
# 1. Get the source
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. Install the base dependencies (viewing, color grading, format handling, and output)
pip install -r requirements.txt

# 3. (Optional) Install the AI and portrait retouching dependencies
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. Start the app
python main.py
```

The Rust acceleration module is optional. If you want it, install the Rust toolchain, build the module, and copy the result to the repository root:

```powershell
cd native\ops_accel
cargo build --release --locked
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

## Building

### Building the Installer

You need Inno Setup, the Rust toolchain, and the full set of dependencies (app, AI, portrait retouching, and packaging). `requirements-build.lock.txt` pins the tested versions and is what release builds install from. `requirements-build.txt` is its source. Before building, the script checks the Python version, whether the dependencies are complete, and that the environment has only one OpenCV distribution.

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

### CI and Releases

CI runs these steps as separate jobs, in order: code checks and Python precompilation, native module build, tests, and installer build. Pull requests run up to the tests. Pushes to `dev`/`main` and manual runs continue on to packaging.

A version tag runs the same stages, checks that the commit is already in `main` and that the tag matches `APP_VERSION`, then creates a draft Release. The maintainer publishes the final release by hand.

The native build uploads `ops_accel.pyd`, and the tests and packaging download the same artifact. Packaging reuses that module through `build.ps1 -SkipNativeBuild` and prepares Cargo dependency sources for license collection. Downstream jobs pick the artifact by the producer's artifact ID.

### Running Tests

For test instructions, see [tests/README.en.md](../tests/README.en.md).

## PySide6 and the Native Module

The base dependencies pin `PySide6_Essentials==6.11.3` and `shiboken6==6.11.3`. Release dependencies follow [requirements-build.lock.txt](../requirements-build.lock.txt), and the Qt binding is PySide6. `build.ps1` always uses `.venv\Scripts\python.exe` in the repository root, and requires all release sources to be committed and the working tree to be clean. Ordinary source runs can happen in a development working tree.

AVX2 is detected at runtime, and release builds keep a portable CPU target. If the native module is missing, a source run can use the Python/NumPy path for the related computation. A full release build requires `ops_accel.pyd` to be built successfully or explicitly reused.

The build script cleans `build/` and `dist/`, so save any outputs you want to keep first. `-SkipNativeBuild` applies only to a native module that has been confirmed to match the candidate source. License collection needs local Cargo dependency sources, which you can prepare by running `cargo fetch --locked`.

Outputs: the program folder `dist/OpenPictureStudio/`, and the EXE and `.exe.sha256` in `dist/installer/`. Before release, complete the manual acceptance checks below and confirm that the native module in the program folder matches this build.

## PySide6 Migration and Release Acceptance

The current source and packaging configuration use PySide6 Essentials and Shiboken 6.11.3, and exclude PyQt5, PyQt6, and PySide2. Before a release, check the following by hand:

1. On a test account, install the previous version and record its version, install folder, and data folder. Back up settings, AI models, `.opsproj` projects, and custom data.
2. Close all OPS instances and install the candidate over it in the same folder. Check that the app starts, that the old PyQt files are removed according to the install manifest, and that PySide6 and Shiboken load normally.
3. Open existing projects and check the edit history, model references, and export. Check that the settings and the custom data folder still work.
4. In Settings, confirm that models and runtimes are still recognized, then open the viewer, RAW, mask, retouching, and assistant interfaces in turn.
5. Cancel one test installation, then run the full installer of the same version. Handle backups and conflicts as the installer prompts, and check that the app starts after the repair.
6. On a machine with AVX2 and one without, check startup, color grading, and export. `portable` can verify the portable kernels. Records of real hardware compatibility must come from the matching machines.

During acceptance, record the installer version, SHA256, Windows version, data folder, and results.
