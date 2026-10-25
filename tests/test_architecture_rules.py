"""分层与依赖方向：界面 → 应用 → 核心，共享控件不反向依赖对话框。"""

import ast
import importlib
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _modules(package: str):
    return sorted(path for path in (SRC / package).rglob("*.py") if "__pycache__" not in path.parts)


def _imports(path: Path):
    """返回 (被导入的完整模块或名字, 行号)，相对导入展开为绝对名。"""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    is_package = path.name == "__init__.py"
    if is_package:
        parts = parts[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = parts if is_package else parts[:-1]
                anchor = anchor[:len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([base] if base else []))
            yield base, node.lineno
            for alias in node.names:
                yield f"{base}.{alias.name}", node.lineno


def _violations(package: str, forbidden: tuple[str, ...], allowed: frozenset = frozenset()):
    found = set()
    for path in _modules(package):
        relative = path.relative_to(ROOT).as_posix()
        for target, _line in _imports(path):
            if target.startswith(forbidden) and (relative, target) not in allowed:
                found.add((relative, target))
    return sorted(found)


def test_core_does_not_depend_on_widgets_or_upper_layers():
    assert _violations("core", ("PyQt6.QtWidgets", "src.application", "src.ui")) == []


def test_application_does_not_depend_on_ui():
    assert _violations("application", ("PyQt6.QtWidgets", "src.ui")) == []


def test_panels_and_widgets_do_not_reach_into_the_color_dialog():
    """共享控件位于 widgets；面板不得再从模态对话框模块取控件。"""
    target = ("src.ui.dialogs.color_adjust_dialog",)
    assert _violations("ui/panels", target) == []
    assert _violations("ui/widgets", target) == []


# 既有的一处反向引用：工具侧栏仅在类型标注中引用面板基类。
KNOWN_WIDGET_EXCEPTIONS = frozenset({
    ("src/ui/widgets/tool_sidebar.py", "src.ui.panels.base_tool_panel"),
    ("src/ui/widgets/tool_sidebar.py", "src.ui.panels.base_tool_panel.BaseToolPanel"),
})


def test_widgets_do_not_gain_new_dependencies_on_dialogs_or_panels():
    assert _violations("ui/widgets", ("src.ui.dialogs", "src.ui.panels"), KNOWN_WIDGET_EXCEPTIONS) == []


def _importable_modules():
    names = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return [name for name in names if name != "src.main"]  # 入口模块不在测试中导入


@pytest.mark.parametrize("name", _importable_modules())
def test_every_module_imports(name):
    """清理或搬移代码后，任何模块都应仍能独立导入。"""
    importlib.import_module(name)
