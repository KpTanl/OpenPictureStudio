# OpenPictureStudio (OPS)

[简体中文](README.md) | [English](README.en.md)

<p align="center">
  <img src="resources/assets/logo.png" width="128" alt="OpenPictureStudio">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white" alt="Python Version">
  <img src="https://img.shields.io/badge/PyQt-6.5%2B-green?logo=qt&logoColor=white" alt="PyQt Version">
  <img src="https://img.shields.io/badge/Rust-Accelerated-orange?logo=rust&logoColor=white" alt="Rust Accelerated">
  <img src="https://img.shields.io/badge/Platform-Windows_10%2F11-0078D6" alt="Platform">
  <img src="https://img.shields.io/badge/Version-v0.16.0-blueviolet" alt="Version">
  <img src="https://img.shields.io/badge/Source_License-MIT-yellow.svg" alt="Source License">
</p>

OpenPictureStudio (OPS) is an open-source photo workspace that brings browsing and culling, RAW color editing, local adjustments, AI-assisted editing, and batch export into one application. Save edits as projects with their history and pick up where you left off.

- 🎨 **Linear-space color editing**: The color pipeline uses float32 linear space throughout, with an optional Rust native module for acceleration and a Python implementation when the native module is unavailable.
- 🗂️ **From selection to delivery**: Browse, cull, rate, flag, organize, edit, adjust groups of photos, and export batches in one window.
- 🤖 **AI assistance**: Image enhancement, background removal, portrait retouching, ID photos, and an AI photo assistant that can apply adjustments directly.
- 🔒 **Offline first**: Core editing works without network services. Source files are preserved by default; output is written to a temporary file and validated before replacing the destination.

> Currently in Beta. Supports Windows 10/11 (64-bit); 8 GB of RAM or more is recommended.

---

## Documentation

| Document | Links |
| --- | --- |
| User guide | [简体中文](README.md) · [English](README.en.md) |
| Installation and source setup | [简体中文](README.md#安装与运行) · [English](#installation-and-running) |
| Installer builds | [简体中文](README.md#构建) · [English](#building) |
| Tests | [简体中文](tests/README.md) · [English](tests/README.en.md) |
| CI and release workflows | [CI](.github/workflows/ci.yml) · [Release](.github/workflows/release.yml) · [Shared environment](.github/actions/setup-build/action.yml) |
| Project license | [MIT License](LICENSE) |

<details>
<summary>Resources, dependencies, and third-party licenses</summary>

- Model catalog: [catalog.json](resources/models/catalog.json).
- Runtime catalog: [catalog.json](resources/runtimes/catalog.json).
- License supplements and corresponding source: [manifest.json](packaging/licenses/manifest.json).
- Bundled notices: [FlatBuffers](packaging/licenses/flatbuffers/LICENSE) · [MediaPipe](packaging/licenses/mediapipe/MEDIAPIPE_THIRD_PARTY_NOTICES.txt) · [Qt](packaging/licenses/qt/QT_THIRD_PARTY_NOTICES.txt) · [MSVC runtime](packaging/licenses/msvc-runtime/NOTICE.txt).
- Model licenses: [MediaPipe](resources/models/licenses/MEDIAPIPE.txt) · [NAFNet](resources/models/licenses/NAFNET.txt) · [Real-ESRGAN](resources/models/licenses/REAL_ESRGAN.txt) · [U2Net](resources/models/licenses/U2NET.txt) · [YuNet](resources/models/licenses/YUNET.txt).
- Interface icons: [Source](resources/icons/ui/SOURCE.txt) · [License](resources/icons/ui/LICENSE.txt).
- Dependency lists: [requirements.txt](requirements.txt) · [requirements-ai.txt](requirements-ai.txt) · [requirements-beauty.txt](requirements-beauty.txt) · [requirements-dev.txt](requirements-dev.txt) · [requirements-build.txt](requirements-build.txt) · [requirements-build.lock.txt](requirements-build.lock.txt).

</details>

---

## Features

### Browsing and Culling

- **Common formats and camera RAW**: Open JPEG, PNG, BMP, GIF, WebP, TIFF, ICO, and PPM/PGM/PBM directly. Camera RAW support includes CR2, CR3, NEF, NRW, ARW, RAF, DNG, ORF, PEF, and RW2 from Canon, Nikon, Sony, Fujifilm, Olympus, Pentax, Panasonic, and other brands.
- **RAW previews first**: Opening a RAW file shows its embedded camera preview before replacing it with the decoded image. RAW display modes include Camera preview, Original Color, and OPS Look. Other formats offer Original Color and OPS Look, with preferences saved separately for RAW and other formats.
- **Filtering and comparison**: Browse in a filmstrip or grid. Add star ratings and Keep/Reject flags; filter by rating, flag, file type, editing status, and delivery status. Compare two photos side by side at their original resolution with linked navigation.
- **Photo library**: Add keywords and organize photos into regular collections or smart collections that update automatically based on rules.
- **Photo information**: View shooting settings, camera and lens details, source file information, and histograms, including for RAW files.
- **Everyday tools**: Rotate, flip, set wallpapers, and associate file types. Choose Dark, Light, or Beige themes and switch the interface between 简体中文, English, and Français.

### Editing

- **Color adjustments**:
  - Temperature, tint, exposure, contrast, highlights, shadows, whites, and blacks.
  - Texture, clarity, dehaze, vibrance, and saturation.
  - RGB and individual channel tone curves, an eight-channel color mixer with an on-image eyedropper, a live histogram, and one-click auto adjustment.
  - 11 built-in presets: Monochrome, Retro, Fresh, Warm, Cool, Soft Light, Vivid, Dramatic, HDR, Film, and Skin Beauty.
- **Local masks**: Linear and radial gradients, brush and eraser, subject selection, facial skin, eyes, lips, background, and the whole photo. Combine multiple regions by adding or subtracting them within a mask, with separate adjustments for each mask.
- **OPS Look**: Choose Original Color or OPS Look for RAW, JPG, PNG, and other formats.
- **Composition**: Free cropping and rotation, grid cropping, shape cropping, and lasso selection.
- **History and projects**: Multi-step undo and redo, a history panel for returning to any step, and an edit stack showing the current processing steps. Press and hold to compare with the original. Save edits as `.opsproj` projects.

### Groups of Photos

- **Group adjustments**: Use one photo as a reference and sync selected adjustments to selected photos or all filtered results, or increase or decrease exposure across the group. Undo or redo each batch, or retry photos that did not complete successfully.
- **Photo groups and calibration**: Get grouping suggestions based on capture time, camera, and lens, along with exposure-matching and RAW white-balance alignment suggestions for each group. Preview the changes before applying them together.

### AI Tools

- **Image enhancement**: Real-ESRGAN super-resolution (2×, 4×, and 4K), NAFNet denoising and deblurring, and restoration at the original dimensions. Adjust luminance and color noise separately.
- **Background removal**: U2Net subject detection, color-based removal, and lasso cutouts. Refine edges and export with a transparent or solid-color background.
- **Portrait retouching**: Facial landmarks drive four groups of adjustments: skin (smoothing, brightening, rosy tone, and sharpening), face shape (face and nose slimming), eyes (enlargement and spacing), and detail retouching (teeth whitening, blemish removal, and under-eye bag reduction).
- **ID photos**: Presets for Chinese one-inch and two-inch ID photos, passports, visas, social security documents, and custom dimensions. Change the background color, frame automatically based on face position, and arrange photos on a print sheet.
- **AI photo assistant**: Multiple chats with Chat, Plan, Adjust, and Agent modes. Adjustments enter the same editing history as manual edits, so you can undo, redo, or refine them. Request two alternatives to generate A/B candidates, compare them in the assistant panel, and add one to history by clicking Apply. Attach a reference image or save named styles and preferences for future use.
- **Models and GPU**: Models are downloaded separately from the installer. Download, import, uninstall, and set defaults in Settings → Tools and Models. If you have a dedicated GPU, install the GPU runtime (DirectML) in Settings → Runtimes and restart the application to activate it.

---

## Installation and Running

### Option A: Installer

Download the latest `OpenPictureStudio-<version>-beta-setup.exe` from the [Releases page](https://github.com/KpTanl/OpenPictureStudio/releases) and run it to install.

By default, the installed application checks GitHub for the latest regular release, with at least 24 hours between automatic checks for each user. Disable automatic checks in Settings → General, or check manually on the About page. Requests include the application version and necessary HTTP information; GitHub can see the network IP address and request time. The application opens a fixed download page for you to download and run the installer. Running from source does not trigger automatic checks; update the source with git.

Exit all running instances normally before upgrading, then close the installer before launching the application. The installer checks program files against its file manifest and preserves settings, custom data directories, models, and projects. If unknown files, user-replaced components, or directory conflicts are found, follow the prompts to back up and move the conflicting content. After an interrupted installation, rerun the full installer for the same version. Back up and resolve any truncated or modified files before continuing the repair.

Early test builds may not have an installation manifest. If their program files remain, back up your data and uninstall the old build first. If the original installation directory has been removed or is completely empty, rerun the installer directly. When replacement is blocked, the installer shows the original directory.

### Option B: Run from Source

Requires Python 3.12 or later; development and release builds use 3.13. Dependencies are grouped by feature, so you can install only what you need.

```powershell
# 1. Get the source
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. Install the base dependencies (viewing, color editing, format handling, and output)
pip install -r requirements.txt

# 3. (Optional) Install AI and portrait retouching dependencies
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. Launch the application
python main.py
```

The Rust acceleration module is optional. To use it, install the Rust toolchain, build the module, and copy the result to the repository root:

```powershell
cd native\ops_accel
cargo build --release
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

### Models

Download or import the models you need in Settings → Tools and Models, then select them in the relevant tool panel. AI background removal and AI background replacement for ID photos use U2Net. Keeping the original background, cropping, resizing, and arranging ID photos require no model. The installed application stores models in `%APPDATA%\OpenPictureStudio\OpenPictureStudio\models` by default. Saved projects restore the exact model versions they reference.

### Logs

Both the installed application and source runs write logs to `%APPDATA%\OpenPictureStudio\OpenPictureStudio\logs\openpicturestudio.log` by default. Change the location, size per log file (about 2 MB by default), and number of rotated files to keep (3 by default) in Settings → Storage. You can also open the log folder there. Attach the log when reporting an issue to help with diagnosis.

---

## AI Services and Privacy

The AI photo assistant uses services you configure yourself. Add connections and models in Settings → AI Services. The assistant sends no requests until a service is configured. API keys are encrypted with Windows DPAPI and stored locally.

Content sent to the service:

- **Text and parameters**: Your messages, current adjustment parameters, and locally computed photo analysis describing brightness, color casts, and faces in text, without image pixels.
- **Photos**: By default, the first message includes a preview of the current image with edits applied, with a maximum long edge of 1536 pixels. The photo selector below the input box can switch to the original image without edits, with a maximum long edge of 2048 pixels, or No photo. Smaller photos are sent at their original dimensions without upscaling. When photo sharing is enabled, the assistant can also request original, task-start, and current previews, plus high-resolution crops allowed by the model settings. All images are JPEGs without shooting metadata. Selecting No photo prevents photos from being sent in any request; photos are also omitted when image input is disabled for the model.
- **Reference images**: Sent only when you attach one.

The top of the assistant panel shows what will be sent for the current request. Chats, attachments, and styles are stored locally.

---

## Projects and Output

- **Projects and drafts**: Edits are saved automatically as drafts so you can continue when you reopen the same photo. Saving an `.opsproj` project preserves processing steps and history.
- **Saving results**: Choose whether to overwrite the original or save a copy. Output is written to a temporary file and validated before replacing the destination, preserving existing files if saving fails or is canceled.
- **Batch export**: Select photos and an output preset, then add them to the export queue. Edits are frozen when queued, so later changes to a photo do not affect queued items. Pause or cancel the queue, retry failed items, and review completion records.
- **Format handling**: Convert between JPG, PNG, WebP, BMP, and TIFF. Set a target file size in KB for JPG. Resize with aspect-ratio preservation, crop-to-fill, padded fit, or stretching.
- **High bit depth and PDF**: Export 16-bit TIFF with transparency, or combine multiple images into a single PDF in order.

---

## Building

### Build the Installer

Requires Inno Setup, the Rust toolchain, and the full application, AI, portrait retouching, and packaging dependencies. `requirements-build.lock.txt` pins tested versions for release builds; `requirements-build.txt` is its source. Before building, the script checks the Python version, required dependencies, and that exactly one OpenCV distribution is installed.

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

### CI and Releases

CI runs code checks and Python compilation, builds the native module, runs the test suite, and packages the installer as separate jobs. Pull requests run through testing; pushes to `dev`/`main` and manual runs also package the installer.

Release tags run the same stages, verify that the commit belongs to `main` and that the tag matches `APP_VERSION`, then create a draft Release. A maintainer publishes the Release manually.

The native build uploads `ops_accel.pyd`. Testing and packaging download the same artifact; packaging uses `build.ps1 -SkipNativeBuild` and prepares Cargo dependency sources for license collection. Each downstream job selects the producer's artifact ID.

### Run Tests

See the [test instructions](tests/README.en.md).

### Code Structure

The application follows a one-way dependency structure: UI → application → core → native acceleration. Each layer depends only on the public contracts of the layer below it.

- **`src/ui`**: Interface and interactions, including browsing and editing workspaces, tool panels, themes, and localization.
- **`src/application`**: Workflow coordination, including task scheduling, caches, model and runtime management, output, and persistence.
- **`src/core`**: Algorithms and data models, including the imaging engine, tool contracts, workflows, and `.opsproj` projects.
- **`native/ops_accel`**: Native acceleration module written in Rust.
- **`resources`**: Themes, translations, and model and runtime catalogs.

---

## License and Third-Party Dependencies

The original code of OpenPictureStudio is released under the [MIT License](LICENSE). Third-party dependencies, models, and assets follow their own licenses. Interface icons come from [Tabler Icons](https://github.com/tabler/tabler-icons) under the MIT License, with the license text stored alongside them in [resources/icons/ui/LICENSE.txt](resources/icons/ui/LICENSE.txt).

Installer and portable distributions include the following materials in the program directory, generated from the actual bundle contents by [tools/collect_licenses.py](tools/collect_licenses.py) during the build:

- `THIRD_PARTY_NOTICES.txt`: Component names, versions, licenses, and homepages, including models and runtimes available for download;
- `licenses/`: License texts and copyright notices for each component;
- `SOURCE.txt`: The application commit and source URL, plus corresponding source URLs and checksums for GPL, LGPL, and MPL components;
- `components.json`: The mapping between components and distributed files;
- `LICENSE` (MIT) and `GPL-3.0.txt`.

License texts missing from upstream packages and components requiring corresponding source are registered in [packaging/licenses](packaging/licenses). The build stops if a distributed file cannot be assigned to a component, a component is missing its license text, or a GPL, LGPL, or MPL component has no corresponding source registration.

This project uses PyQt6, whose open-source edition is licensed under GPLv3; commercial licensing is also available. A combined application built and distributed with GPL-licensed PyQt6 must be distributed under GPLv3. The MIT license for the project's original code remains in effect: MIT applies when reusing that code on its own, while reusing and distributing the combined application with GPL-licensed PyQt6 also requires compliance with GPLv3.

When distributing such installers or portable packages, retain the applicable copyright notices and license texts, and provide the complete corresponding source for the release, including source for GPL-covered dependencies and the necessary build scripts. Access to the corresponding source should be provided alongside the binary download.

For detailed terms, see the [official PyQt licensing information](https://www.riverbankcomputing.com/software/pyqt/intro), [PyQt licensing FAQ](https://www.riverbankcomputing.com/commercial/license-faq), and [GPLv3 terms](https://www.gnu.org/licenses/gpl-3.0.html). Distributions using a commercial PyQt license follow its commercial terms and the licenses of other dependencies.

---
<p align="center">
  Original project code released under the <b>MIT License</b><br>
  Thanks to the open-source communities behind PyQt6, OpenCV, Pillow, LibRaw, ONNX Runtime, Real-ESRGAN, NAFNet, U2Net, MediaPipe, and others
</p>
