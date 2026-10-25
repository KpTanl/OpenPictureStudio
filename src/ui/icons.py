"""界面图标：按名称加载 resources/icons/ui 下的线性 SVG，绘制时按当前主题取色。

图标在每次绘制时读取主题取值，主题切换后随控件重绘自动换色，调用方无需重新设置。
"""

from __future__ import annotations

import re
from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QIconEngine, QPainter, QPixmap, QPixmapCache
from PyQt6.QtSvg import QSvgRenderer

from ..utils.resource_paths import resolve_resource
from .theme import get_theme_tokens

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_STROKE_WIDTH = "1.75"
_MODE_ROLES = {
    QIcon.Mode.Normal: "text_secondary",
    QIcon.Mode.Active: "text_primary",
    QIcon.Mode.Selected: "text_primary",
    QIcon.Mode.Disabled: "text_disabled",
}


@lru_cache(maxsize=None)
def _source(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError(f"图标名称无效: {name!r}")
    path = resolve_resource(f"icons/ui/{name}.svg")
    if not path.is_file():
        raise FileNotFoundError(f"缺少图标资源: {name}")
    return path.read_text(encoding="utf-8").replace('stroke-width="2"', f'stroke-width="{_STROKE_WIDTH}"')


def _render(name: str, width: int, height: int, color: str) -> QPixmap:
    key = f"ops-icon/{name}/{width}x{height}/{color}"
    cached = QPixmapCache.find(key)
    if cached is not None and not cached.isNull():
        return cached
    pixmap = QPixmap(max(1, width), max(1, height))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    QSvgRenderer(QByteArray(_source(name).replace("currentColor", color).encode("utf-8"))).render(
        painter, QRectF(0, 0, pixmap.width(), pixmap.height()))
    painter.end()
    QPixmapCache.insert(key, pixmap)
    return pixmap


class _ThemedIconEngine(QIconEngine):
    def __init__(self, name: str, role: str | None, checked_role: str | None):
        super().__init__()
        self._name = name
        self._role = role
        self._checked_role = checked_role

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> str:
        tokens = get_theme_tokens()
        if mode == QIcon.Mode.Disabled:
            return tokens[_MODE_ROLES[mode]]
        if self._checked_role is not None and state == QIcon.State.On:
            return tokens[self._checked_role]
        if self._role is not None:
            return tokens[self._role]
        return tokens[_MODE_ROLES[mode]]

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        ratio = painter.device().devicePixelRatioF()
        painter.drawPixmap(rect, _render(self._name, round(rect.width() * ratio), round(rect.height() * ratio),
                                         self._color(mode, state)))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return QPixmap(_render(self._name, size.width(), size.height(), self._color(mode, state)))

    def clone(self) -> QIconEngine:
        return _ThemedIconEngine(self._name, self._role, self._checked_role)


def themed_icon(name: str, *, role: str | None = None, checked_role: str | None = None) -> QIcon:
    """按名称取界面图标。

    默认按状态取文字色：常态为次要文字，悬停与选中为主要文字，禁用为禁用文字；
    role 指定语义取值的键（如 "accent"、"danger"、"on_accent"）时，非禁用状态都用该颜色；
    checked_role 指定可选中按钮处于选中态时的取值。
    """
    _source(name)
    tokens = get_theme_tokens()
    for key in (role, checked_role):
        if key is not None and key not in tokens:
            raise KeyError(f"未知的颜色取值: {key}")
    return QIcon(_ThemedIconEngine(name, role, checked_role))
