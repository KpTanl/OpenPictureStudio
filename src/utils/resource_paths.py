from __future__ import annotations

import sys
from pathlib import Path


_ICON_SYNC_DONE = False


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parents[2])
    roots.append(Path.cwd())

    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique_roots.append(root)
    return unique_roots


def _resolve_from_candidates(candidate_rel_paths: list[Path], fallback: Path) -> Path:
    for root in _candidate_roots():
        for rel_path in candidate_rel_paths:
            candidate = root / rel_path
            if candidate.exists() and candidate.is_file():
                return candidate
    return fallback


def resolve_resource(relative: str) -> Path:
    """定位 resources 目录下的文件；源码运行与发行包共用，找不到时返回源码树中的路径。"""
    rel_path = Path(relative)
    return _resolve_from_candidates(
        [Path("resources") / rel_path, rel_path],
        Path(__file__).resolve().parents[2] / "resources" / rel_path,
    )


def _sync_app_icon_from_logo_once() -> None:
    global _ICON_SYNC_DONE
    if _ICON_SYNC_DONE:
        return
    _ICON_SYNC_DONE = True

    try:
        from PIL import Image
    except Exception:
        return

    relative_pairs = [
        (Path("resources/assets/logo.png"), Path("resources/icons/app.ico")),
        (Path("assets/logo.png"), Path("icons/app.ico")),
    ]

    for root in _candidate_roots():
        for logo_rel, icon_rel in relative_pairs:
            logo_path = root / logo_rel
            icon_path = root / icon_rel

            if not logo_path.exists() or not logo_path.is_file():
                continue

            try:
                logo_mtime = logo_path.stat().st_mtime
                icon_mtime = icon_path.stat().st_mtime if icon_path.exists() else -1.0
            except OSError:
                continue

            if icon_mtime >= logo_mtime:
                continue

            try:
                icon_path.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(logo_path) as image:
                    image.convert("RGBA").save(
                        icon_path,
                        format="ICO",
                        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)],
                    )
                return
            except Exception:
                continue


def resolve_app_icon_path() -> Path:
    """Resolve shell/app icon path, preferring .ico for Windows integrations."""
    _sync_app_icon_from_logo_once()

    candidate_rel_paths = [
        Path("resources/icons/app.ico"),
        Path("resources/assets/logo2.png"),
        Path("resources/assets/logo.png"),
        Path("icons/app.ico"),
        Path("assets/logo2.png"),
        Path("assets/logo.png"),
    ]

    return _resolve_from_candidates(
        candidate_rel_paths,
        Path(__file__).resolve().parents[2] / "resources" / "icons" / "app.ico",
    )
