# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — OpenPictureStudio full AI build (windowed, onedir)
# Build via:  powershell -ExecutionPolicy Bypass -File build.ps1
# Or directly: .venv\Scripts\pyinstaller.exe OpenPictureStudio.spec
#
# AI models are NOT bundled — the app downloads them on first use (or the user
# places them manually) into %APPDATA%\OpenPictureStudio\OpenPictureStudio\models.

import json
from fnmatch import fnmatch

from PyInstaller.utils.hooks import collect_submodules, collect_all, copy_metadata

datas = [
    ("resources", "resources"),
]
binaries = []
if os.path.exists("ops_accel.pyd"):
    binaries.append(("ops_accel.pyd", "."))
hiddenimports = collect_submodules("rawpy")
hiddenimports += collect_submodules("tifffile")
hiddenimports += collect_submodules("src.ui.panels")
hiddenimports += collect_submodules("httpx")
hiddenimports += ["anyio._backends._asyncio", "httpcore._backends.asyncio"]
datas += copy_metadata("httpx", recursive=True)
_d, _b, _h = collect_all("certifi")
datas += _d
binaries += _b
hiddenimports += _h


# mediapipe / rembg / onnxruntime + rembg's matting/fetch deps ship native
# binaries and data files that PyInstaller's static analysis misses.
for _pkg in ("cv2", "mediapipe", "rembg", "onnxruntime", "pymatting", "pooch"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# rembg / pymatting / pooch query importlib.metadata at import time; without
# the .dist-info bundled this raises PackageNotFoundError in the frozen app.
# recursive=True pulls metadata for the whole rembg dependency chain.
datas += copy_metadata("rembg", recursive=True)
datas += copy_metadata("onnxruntime")
datas += copy_metadata("mediapipe")

excludes = [
    "torch",
    "tkinter",
    "pytest",
    "black",
    "flake8",
    "mypy",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtBluetooth",
    "PyQt6.QtNfc",
    "PyQt6.QtPositioning",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # img2pdf 是 LGPL：以 .py 文件交付，用户可以替换
    module_collection_mode={"img2pdf": "py"},
)


# QtPdf (PDFium) comes in with the Qt image format plugins; the app never reads PDFs through Qt.
_EXCLUDED_NATIVE = {"opengl32sw.dll", "qt6pdf.dll", "qpdf.dll"}


def _keep_native_file(entry):
    for path in entry[:2]:
        name = os.path.basename(path).lower()
        if name in _EXCLUDED_NATIVE or fnmatch(name, "opencv_videoio_ffmpeg*.dll"):
            return False
    return True


a.binaries = [entry for entry in a.binaries if _keep_native_file(entry)]
a.datas = [entry for entry in a.datas if _keep_native_file(entry)]

# 实际打包清单，供 tools/collect_licenses.py 归属组件；含本机路径，只留在构建目录
_toc_path = os.path.join(SPECPATH, "build", "bundle_toc.json")
os.makedirs(os.path.dirname(_toc_path), exist_ok=True)
with open(_toc_path, "w", encoding="utf-8") as _toc:
    json.dump({kind: [list(entry[:3]) for entry in getattr(a, kind)]
               for kind in ("pure", "scripts", "binaries", "datas")}, _toc, ensure_ascii=False)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenPictureStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无控制台黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="resources/icons/app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OpenPictureStudio",
)
