"""界面代码里不出现写死的颜色、字号和圆角：颜色取语义取值，字号与圆角取档位常量。"""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "color": re.compile(r"(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|\brgba?\(|QColor\(\s*\d"),
    "font": re.compile(r"font-size:\s*\d|setPixelSize\(\s*\d|setPointSizeF?\(\s*\d"),
    "radius": re.compile(r"border-radius:\s*\d"),
}


def find_literals():
    found = []
    for path in sorted((ROOT / "src" / "ui").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(ROOT).as_posix()}:{number} {kind}: {line.strip()}")
    return found


def test_ui_code_has_no_literal_colors_font_sizes_or_radii():
    found = find_literals()
    assert not found, "界面代码里有写死的颜色、字号或圆角，请改用主题取值与档位常量：\n" + "\n".join(found)


def test_global_stylesheet_takes_colors_and_font_sizes_from_tokens():
    sys.path.insert(0, str(ROOT))
    from resources.styles import theme_variables

    source = inspect.getsource(theme_variables._build_qss)
    assert not PATTERNS["color"].findall(source)
    assert not re.findall(r"font-size:\s*\d", source)
    assert set(re.findall(r"font-weight:\s*(\d+)", source)) <= {"400", "600"}
