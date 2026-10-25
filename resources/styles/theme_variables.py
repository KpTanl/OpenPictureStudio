"""OpenPictureStudio 主题变量定义。

所有 UI 配色、间距、字体、圆角等视觉 token 集中在此维护，
便于统一风格、支持动态主题切换以及未来自定义主题色。
"""

from __future__ import annotations

from resources.styles.semantic_colors import (CANVAS_BACKGROUNDS, MIXER_CHANNEL_COLORS, SEMANTIC_BEIGE,
                                              SEMANTIC_DARK, SEMANTIC_LIGHT, brand_gradient_states)

# =============================================================================
# 字体
# =============================================================================
FONT_FAMILIES = ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei")
FONT_FAMILY = ", ".join(f"'{family}'" for family in FONT_FAMILIES) + ", sans-serif"
FONT_FAMILY_MONO = "'SF Mono', 'Cascadia Code', 'Consolas', 'Courier New', monospace"
# 字号四档：说明、正文、标题、页标题；字重只用 400 与 600
FONT_SIZE_CAPTION = "12px"
FONT_SIZE_BODY = "14px"
FONT_SIZE_TITLE = "16px"
FONT_SIZE_PAGE_TITLE = "20px"

# =============================================================================
# 圆角与间距
# =============================================================================
# 小控件、控件、面板与对话框；胶囊取默认控件高度的一半（超过半高时 Qt 不绘制圆角）
RADIUS_SMALL = "6px"
RADIUS_CONTROL = "8px"
RADIUS_PANEL = "12px"
RADIUS_PILL = "16px"

SPACING_XS = "4px"
SPACING_SM = "8px"
SPACING_MD = "12px"
SPACING_LG = "16px"
SPACING_XL = "24px"

# =============================================================================
# 尺寸
# =============================================================================
# 控件总高（逻辑像素）：默认、紧凑（标签与小按钮）、工具条图标按钮
CONTROL_HEIGHT = 32
CONTROL_HEIGHT_COMPACT = 28
TOOL_BUTTON_SIZE = 34
TOP_BAR_HEIGHT = 48
# 底部工具条：总高，以及它与画布下沿的间距；两者之和是画布在适应窗口时让出的高度
CANVAS_TOOLBAR_HEIGHT = 42
CANVAS_TOOLBAR_MARGIN = 14
# 胶片条的默认总高
FILMSTRIP_HEIGHT = 78
FILMSTRIP_HEIGHT_COMPACT = 64
FILMSTRIP_HEIGHT_WIDE = 96
# 右侧照片信息栏的宽度；编辑工作区左侧工具栏的宽度
INFO_SIDEBAR_WIDTH = 280
INFO_SIDEBAR_WIDTH_WIDE = 308
EDIT_SIDEBAR_WIDTH = 308
EDIT_SIDEBAR_WIDTH_WIDE = 336
# 右侧栏可拖动的范围
SIDEBAR_MIN_WIDTH = 280
SIDEBAR_MAX_WIDTH = 420
# 窗口宽度的两个分界：不足前者时右侧栏以浮层展开，达到后者时栏宽与缩略图放大一档
NARROW_WINDOW_WIDTH = 1280
WIDE_WINDOW_WIDTH = 2560
ASSISTANT_INPUT_HEIGHT = 44
ASSISTANT_INPUT_MAX_HEIGHT = 152
ASSISTANT_REFERENCE_EDGE = 48
ASSISTANT_VARIANT_HEIGHT = 280
ASSISTANT_VARIANT_SUMMARY_HEIGHT = 72
ASSISTANT_USAGE_DIAMETER = 14
ASSISTANT_USAGE_STROKE = 2
ASSISTANT_COMMAND_WIDTH = 300
ASSISTANT_CONTEXT_EDIT_HEIGHT = 112
ASSISTANT_MODEL_LIST_HEIGHT = 176
ASSISTANT_SETTINGS_FIELD_WIDTH = 200
ASSISTANT_NAVIGATION_WIDTH = 216
ASSISTANT_CONTEXT_WIDTH = 296
ASSISTANT_READING_WIDTH = 720
ASSISTANT_NAVIGATION_BREAKPOINT = 700
ASSISTANT_CONTEXT_BREAKPOINT = 1080
TOOL_RAIL_WIDTH = 72
# 样式表的 min-height 作用于内容区：总高减去 1px 边框，输入框再减去上下各 4px 内边距
CONTROL_INNER = f"{CONTROL_HEIGHT - 2}px"
COMPACT_INNER = f"{CONTROL_HEIGHT_COMPACT - 2}px"
COMPACT_PILL = f"{CONTROL_HEIGHT_COMPACT // 2}px"
INPUT_INNER = f"{CONTROL_HEIGHT - 2 - 8}px"
SCROLLBAR_SIZE = "6px"
SCROLLBAR_MIN_LEN = "28px"
COMBO_DROPDOWN_WIDTH = "28px"

def px(value: str) -> int:
    """把 "12px" 这样的档位常量转为整数像素，供布局与自绘使用。"""
    return int(value.removesuffix("px"))


# =============================================================================
# 对话框窗口尺寸预设（统一管理各对话框的默认尺寸）
# =============================================================================
DIALOG_SIZES = {
    "settings": {"min_w": 760, "min_h": 520, "default_w": 960, "default_h": 640},
    "storage": {"min_w": 480, "min_h": 220, "default_w": 680, "default_h": 520},
}

# =============================================================================
# 缩放参数（统一管理各画布的缩放限制与步长）
# =============================================================================
ZOOM_LIMITS = {
    "canvas_default": {"min": 0.05, "max": 5.0, "step": 1.2, "spin_range": (5, 1000)},
    "canvas_wide": {"min": 0.05, "max": 10.0, "step": 1.25, "spin_range": (5, 1000)},
    "lasso": {"min": 0.05, "max": 8.0, "step": 1.2, "spin_range": (5, 800)},
    "crop_canvas": {"min": 0.1, "max": 4.0, "step": 1.1, "spin_range": (5, 1000)},
    "preview_wheel": {"step_in": 1.15, "step_out": 0.87, "min": 0.05, "max": 5.0},
    "shape_crop_canvas": {"min": 0.05, "max": 10.0, "step": 1.25, "spin_range": (5, 1000)},
}

# =============================================================================
# 图像导出质量与压缩参数
# =============================================================================
JPEG_QUALITY_DEFAULT = 95

# =============================================================================
# 性能调优参数
# =============================================================================
THUMBNAIL_CACHE_SIZE = 500
COLOR_PREVIEW_TIMER_MS = 140
PREVIEW_PROXY_MAX_PX = 1280
MAX_IMAGE_PIXELS = 500_000_000
THREAD_POOL_MAX_THREADS = 2

# =============================================================================
# 图像处理默认阈值
# =============================================================================
CROP_INITIAL_SELECTION_RATIO = 0.8

# =============================================================================
# 文件名前后缀约定
# =============================================================================
FILE_PREFIX_DEFAULTS = {
    "crop": "cropped",
    "grid_crop": "grid",
    "shape_crop": "shape",
    "compress": "_compressed.jpg",
    "denoise": "_denoise",
    "color_adjust": "_adjusted",
}

# =============================================================================
# 画布渲染颜色（RGB / RGBA 元组，供 QPainter 使用）
# =============================================================================
# 与 QSS token 不同，这些颜色不走样式表，而是直接传给 QPainter / QColor，
# 用于裁切框、网格线、套索路径、ID 照片引导线等"画在图像上的覆盖物"。
#
# 设计：保持两套独立色板，按当前主题（dark / light）自动切换。
# - CANVAS_COLORS_DARK：原始深色画布配色（基准）
# - CANVAS_COLORS_LIGHT：仅覆盖必要的差异项，其余继承 DARK
# - CANVAS_COLORS：主题感知代理对象，下标访问自动按当前主题取色，
#   现有 callsite 写法 ``CANVAS_COLORS["bg_base"]`` 无需修改即可跟随主题切换。

CANVAS_COLORS_DARK: dict = {
    "bg_base": (21, 26, 32),
    "bg_lighter": (42, 42, 42),
    "bg_grid": (45, 45, 45),
    "overlay_dim": (0, 0, 0, 140),
    "selection_border": (0, 120, 212),
    "selection_ruler": (255, 255, 255, 80),
    "handle_border": (255, 255, 255),
    "handle_fill": (0, 120, 212),
    "handle_drag_fill": (255, 80, 80),
    "handle_node_add": (30, 220, 150),
    "handle_node_subtract": (255, 200, 120),
    "region_fill_add": (0, 120, 212, 58),
    "region_fill_subtract": (255, 150, 40, 52),
    "region_stroke_add": (0, 190, 255),
    "region_stroke_subtract": (255, 170, 75),
    "region_draft_fill": (0, 120, 212),
    "region_draft_stroke_add": (0, 220, 255),
    "region_draft_stroke_subtract": (255, 190, 120),
    "region_close_hint_stroke": (255, 240, 120),
    "region_close_hint_fill": (255, 240, 120, 96),
    "grid_line_default": (0, 220, 130),
    "grid_line_selected": (255, 165, 0),
    "grid_line_hover": (255, 255, 255, 200),
    "grid_cell_fill_a": (0, 120, 212, 35),
    "grid_cell_fill_b": (255, 200, 50, 35),
    "grid_cell_text": (255, 255, 255, 180),
    "preview_checker_a": (255, 255, 255),
    "preview_checker_b": (205, 205, 205),
    "placeholder_thumb": (60, 60, 60),
    "error_thumb": (80, 30, 30),
    "shape_dim_overlay": (0, 0, 0, 140),
    "shape_selection_border": (0, 140, 255),
    "shape_hover_border": (255, 255, 255, 180),
    "shape_region_border": (255, 255, 255, 90),
    "shape_label_bg": (0, 120, 212, 220),
    "shape_label_text": (255, 255, 255),
    "shape_handle_fill": (0, 120, 212),
    "shape_handle_drag_fill": (255, 80, 80),
}

# Light 画布只列出与 dark 不同的键；其余直接复用 dark 值（彩色 accent
# 在浅色背景上同样可读，无需改动）。
_LIGHT_CANVAS_OVERRIDES: dict = {
    # 纯背景色：dark→light 翻转
    "bg_base": (243, 245, 248),
    "bg_lighter": (250, 251, 253),
    "bg_grid": (238, 240, 244),
    "placeholder_thumb": (220, 222, 226),
    "error_thumb": (240, 200, 200),
    # 原本依赖"白色"在深色画布上呈现的元素：翻成深色
    "selection_ruler": (40, 40, 40, 100),
    "handle_border": (60, 60, 60),
    "grid_line_hover": (60, 60, 60, 200),
    "grid_cell_text": (40, 40, 40, 200),
    "shape_hover_border": (60, 60, 60, 200),
    "shape_region_border": (60, 60, 60, 110),
    # 亮色 accent 在浅色画布上对比度不足：略压暗
    "grid_line_default": (0, 180, 100),
    "grid_line_selected": (220, 130, 0),
}

CANVAS_COLORS_LIGHT: dict = {**CANVAS_COLORS_DARK, **_LIGHT_CANVAS_OVERRIDES}

# Beige 画布颜色 - 温暖的米黄色调
_BEIGE_CANVAS_OVERRIDES: dict = {
    # 纯背景色：温暖的米黄色
    "bg_base": (245, 240, 232),
    "bg_lighter": (250, 247, 242),
    "bg_grid": (240, 235, 225),
    "placeholder_thumb": (225, 218, 205),
    "error_thumb": (240, 200, 200),
    # 深色元素在米黄色背景上
    "selection_ruler": (60, 50, 40, 100),
    "handle_border": (80, 70, 55),
    "grid_line_hover": (80, 70, 55, 200),
    "grid_cell_text": (60, 50, 40, 200),
    "shape_hover_border": (80, 70, 55, 200),
    "shape_region_border": (80, 70, 55, 110),
    # 金色 accent 在米黄色画布上
    "grid_line_default": (180, 150, 80),
    "grid_line_selected": (200, 160, 60),
}

CANVAS_COLORS_BEIGE: dict = {**CANVAS_COLORS_DARK, **_BEIGE_CANVAS_OVERRIDES}


class _CanvasColorsView:
    """主题感知的画布配色代理对象。

    所有 dict 风格访问 (``[key]`` / ``.get()`` / ``in`` / 迭代等) 会在
    调用时刻动态查询当前主题，返回对应的 dark / light / beige 值。

    判定规则：
    - ``QApplication.property("ops.theme")`` 以 ``"light"`` 开头 → CANVAS_COLORS_LIGHT
    - ``QApplication.property("ops.theme")`` 以 ``"beige"`` 开头 → CANVAS_COLORS_BEIGE
    - 其他情况 → CANVAS_COLORS_DARK
    """

    __slots__ = ()

    @staticmethod
    def _current() -> dict:
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                name = app.property("ops.theme")
                if isinstance(name, str):
                    if name.startswith("light"):
                        return CANVAS_COLORS_LIGHT
                    elif name.startswith("beige"):
                        return CANVAS_COLORS_BEIGE
        except Exception:
            pass
        return CANVAS_COLORS_DARK

    def __getitem__(self, key):
        return self._current()[key]

    def get(self, key, default=None):
        return self._current().get(key, default)

    def __contains__(self, key):
        return key in self._current()

    def __iter__(self):
        return iter(self._current())

    def __len__(self):
        return len(self._current())

    def keys(self):
        return self._current().keys()

    def items(self):
        return self._current().items()

    def values(self):
        return self._current().values()

    def __repr__(self):
        return f"<CANVAS_COLORS theme-aware proxy: {len(self)} keys>"


# 向后兼容：保留 CANVAS_COLORS 作为入口名，但底层切成代理对象
# 现有 ``CANVAS_COLORS["bg_base"]`` 写法继续工作并自动跟随主题
CANVAS_COLORS = _CanvasColorsView()


def get_canvas_color(key: str, fallback=None):
    """显式 API：按当前主题返回画布颜色元组（RGB 或 RGBA）。

    与 ``CANVAS_COLORS[key]`` 等价，提供更清晰的调用语义。
    新代码推荐使用此 API。
    """
    return CANVAS_COLORS.get(key, fallback)


# 校验 dark / light 画布键集合一致，防止漏键运行时 KeyError
assert frozenset(CANVAS_COLORS_DARK.keys()) == frozenset(CANVAS_COLORS_LIGHT.keys()), (
    "CANVAS_COLORS_DARK / LIGHT key mismatch: "
    f"only in DARK={sorted(set(CANVAS_COLORS_DARK) - set(CANVAS_COLORS_LIGHT))}, "
    f"only in LIGHT={sorted(set(CANVAS_COLORS_LIGHT) - set(CANVAS_COLORS_DARK))}"
)

# =============================================================================
# 主题取值：语义键见 semantic_colors.py
# =============================================================================
DARK = dict(SEMANTIC_DARK)
LIGHT = dict(SEMANTIC_LIGHT)
BEIGE = dict(SEMANTIC_BEIGE)


def _gradient(colors: tuple[str, str, str]) -> str:
    """品牌渐变的样式表写法：自左向右略向下倾，中间色位于 58%。"""
    first, middle, last = colors
    return f"qlineargradient(x1:0, y1:0, x2:1, y2:0.25, stop:0 {first}, stop:0.58 {middle}, stop:1 {last})"


def _build_qss(theme: dict) -> str:
    """根据主题变量字典生成完整 QSS 字符串。"""
    t = theme
    brand = {state: _gradient(stops) for state, stops in brand_gradient_states(t["surface"]).items()}
    mixer_dots = "".join(
        f'\nQPushButton#mixerChannelDot[channel="{channel}"] {{ background-color: {color}; }}\n'
        for channel, color in MIXER_CHANNEL_COLORS.items())
    return f"""
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY};
    color: {t["text_primary"]};
}}

QMainWindow {{
    background-color: {t["surface"]};
}}

QDialog {{
    background-color: {t["surface"]};
    border: none;
    border-radius: 0px;
}}

QLabel {{
    background-color: transparent;
    color: {t["text_primary"]};
}}

QLabel#dialogTitle {{
    color: {t["text_primary"]};
    font-size: {FONT_SIZE_TITLE};
    font-weight: 600;
    padding: {SPACING_XS} 0 {SPACING_SM} 0;
}}

/* 消息与进度对话框：标题下是正文，再下是字号小一档的补充说明 */
QLabel#dialogMessage,
QLabel#usageBarText {{
    color: {t["text_secondary"]};
}}

QLabel#dialogDetail {{
    color: {t["text_caption"]};
}}

QLabel#dialogDetail,
QLabel#usageBarText {{
    font-size: {FONT_SIZE_CAPTION};
}}

/* 设置窗口：左侧导航与顶栏同一层底色，右沿一条细线；右侧是页面底色上的表单行 */
QFrame#settingsNavigation {{
    background-color: {t["surface"]};
    border: none;
    border-right: 1px solid {t["hairline_solid"]};
}}

QWidget#settingsPage {{
    background-color: {t["page"]};
}}

QLabel#settingsPageTitle {{
    font-size: {FONT_SIZE_PAGE_TITLE};
    font-weight: 600;
}}

QLabel#settingSectionTitle {{
    font-weight: 600;
    padding-bottom: {SPACING_SM};
}}

QFrame#settingRow {{
    border: none;
    border-top: 1px solid {t["hairline_solid"]};
}}

QLabel#settingRowDescription,
QLabel#settingRowMessage {{
    font-size: {FONT_SIZE_CAPTION};
    color: {t["text_caption"]};
}}

QLabel#settingRowTitle:disabled,
QLabel#settingRowDescription:disabled {{
    color: {t["text_disabled"]};
}}

QLabel#settingRowMessage[kind="warning"] {{
    color: {t["warning"]};
}}

QLabel#settingRowMessage[kind="error"] {{
    color: {t["danger"]};
}}

QLabel#settingRowMessage[kind="success"] {{
    color: {t["success"]};
}}

QLabel#settingRowPath {{
    font-family: {FONT_FAMILY_MONO};
    font-size: {FONT_SIZE_CAPTION};
    color: {t["text_secondary"]};
}}

/* 列表框：对话框里的圆角列表；表头放全选与已选数量，条目之间是内缩的细线 */
QFrame#listBox {{
    background-color: {t["page"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_PANEL};
}}

QFrame#listBox QScrollArea,
QFrame#listBox QScrollArea > QWidget,
QFrame#listBox QScrollArea::viewport {{
    background-color: transparent;
    border: none;
}}

QFrame#listBoxHeader {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QFrame#listBox QFrame#settingRow[first="true"] {{
    border-top: none;
}}

/* 图库：三栏之间一条细线；照片表没有网格，行之间是细线，选中行为强调色浅底 */
QSplitter#librarySplitter::handle {{
    background-color: {t["hairline_solid"]};
}}

QWidget#libraryPane QScrollArea,
QWidget#libraryPane QScrollArea > QWidget,
QWidget#libraryPane QScrollArea::viewport,
QScrollArea#libraryRules,
QScrollArea#libraryRules > QWidget,
QScrollArea#libraryRules::viewport {{
    background-color: transparent;
    border: none;
}}

QFrame#libraryFooter {{
    border: none;
    border-top: 1px solid {t["hairline_solid"]};
}}

QTableWidget#libraryTable {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
    outline: 0px;
    selection-background-color: transparent;
}}

QTableWidget#libraryTable::item {{
    padding: 0px {SPACING_SM};
    border: none;
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QTableWidget#libraryTable QHeaderView {{
    background-color: transparent;
    border: none;
}}

QTableWidget#libraryTable QHeaderView::section {{
    background-color: transparent;
    color: {t["text_caption"]};
    font-size: {FONT_SIZE_CAPTION};
    font-weight: 600;
    border: none;
    border-bottom: 1px solid {t["hairline_solid"]};
    padding: {SPACING_SM} {SPACING_MD};
}}

/* 画面区：对话框里放照片或结果预览的圆角底板，里面的滚动区不另填底色 */
QFrame#previewWell {{
    background-color: {t["well"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_PANEL};
}}

QFrame#previewWell QScrollArea,
QFrame#previewWell QScrollArea > QWidget,
QFrame#previewWell QScrollArea::viewport {{
    background-color: transparent;
    border: none;
}}

/* 快捷键录入框：平时像输入框，录入中为强调色描边 */
QPushButton#shortcutEdit {{
    background-color: {t["page"]};
    color: {t["text_primary"]};
    border: 1px solid {t["track"]};
    border-radius: {RADIUS_CONTROL};
    padding: 0 {SPACING_MD};
    font-family: {FONT_FAMILY_MONO};
    font-weight: 400;
}}

QPushButton#shortcutEdit:hover {{
    border-color: {t["text_caption"]};
}}

QPushButton#shortcutEdit:focus {{
    border-color: {t["accent"]};
}}

QPushButton#shortcutEdit[state="empty"] {{
    color: {t["text_caption"]};
}}

QPushButton#shortcutEdit[state="recording"] {{
    background-color: {t["accent_soft"]};
    border-color: {t["accent"]};
    color: {t["accent"]};
}}

QLabel#mutedText {{
    color: {t["text_caption"]};
}}

QGroupBox {{
    color: {t["text_secondary"]};
    border: 1px solid {t["hairline"]};
    border-radius: {RADIUS_PANEL};
    margin-top: {SPACING_MD};
    padding-top: {SPACING_LG};
    background-color: {t["page"]};
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: {SPACING_MD};
    padding: 0 {SPACING_SM};
    color: {t["text_secondary"]};
}}

/* 次要按钮（描边）：未指定对象名的按钮同此 */
QPushButton,
QPushButton#appleButton {{
    background-color: transparent;
    color: {t["text_primary"]};
    border: 1px solid {t["track"]};
    border-radius: {RADIUS_CONTROL};
    padding: 0 {SPACING_MD};
    min-height: {CONTROL_INNER};
    font-weight: 400;
}}

QPushButton:hover,
QPushButton#appleButton:hover {{
    background-color: {t["control"]};
}}

QPushButton:pressed,
QPushButton#appleButton:pressed {{
    background-color: {t["well"]};
}}

QPushButton:focus,
QPushButton#appleButton:focus {{
    border-color: {t["accent"]};
}}

QPushButton:disabled,
QPushButton#appleButton:disabled {{
    background-color: transparent;
    color: {t["text_disabled"]};
    border-color: {t["hairline_solid"]};
}}

/* 确认按钮（强调色） */
QPushButton#primary,
QPushButton#primaryBtn,
QPushButton#applePrimaryButton {{
    background-color: {t["accent"]};
    color: {t["on_accent"]};
    border-color: {t["accent"]};
    font-weight: 600;
}}

QPushButton#primary:hover,
QPushButton#primaryBtn:hover,
QPushButton#applePrimaryButton:hover {{
    background-color: {t["accent_hover"]};
    border-color: {t["accent_hover"]};
}}

QPushButton#primary:pressed,
QPushButton#primaryBtn:pressed,
QPushButton#applePrimaryButton:pressed {{
    background-color: {t["accent_pressed"]};
    border-color: {t["accent_pressed"]};
}}

QPushButton#primary:focus,
QPushButton#primaryBtn:focus,
QPushButton#applePrimaryButton:focus {{
    border-color: {t["text_primary"]};
}}

QPushButton#primary:disabled,
QPushButton#primaryBtn:disabled,
QPushButton#applePrimaryButton:disabled {{
    background-color: {t["accent_disabled"]};
    color: {t["on_accent_disabled"]};
    border-color: {t["accent_disabled"]};
}}

/* 主操作按钮（品牌渐变）：每个窗口只用于唯一的主操作 */
QPushButton#appleBrandButton {{
    background: {brand["rest"]};
    background-origin: border;
    background-clip: border;
    color: {t["on_brand"]};
    border-color: transparent;
    font-weight: 600;
}}

QPushButton#appleBrandButton:hover {{
    background: {brand["hover"]};
}}

QPushButton#appleBrandButton:pressed {{
    background: {brand["pressed"]};
}}

QPushButton#appleBrandButton:focus {{
    border-color: {t["text_primary"]};
}}

QPushButton#appleBrandButton:disabled {{
    background: {brand["disabled"]};
    color: {t["on_filled_disabled"]};
    border-color: transparent;
}}

/* 危险按钮 */
QPushButton#dangerBtn,
QPushButton#danger,
QPushButton#appleDestructiveButton {{
    background-color: {t["danger"]};
    color: {t["on_danger"]};
    border-color: {t["danger"]};
    font-weight: 600;
}}

QPushButton#dangerBtn:hover,
QPushButton#danger:hover,
QPushButton#appleDestructiveButton:hover,
QPushButton#dangerBtn:pressed,
QPushButton#danger:pressed,
QPushButton#appleDestructiveButton:pressed {{
    background-color: {t["danger_hover"]};
    border-color: {t["danger_hover"]};
}}

QPushButton#dangerBtn:focus,
QPushButton#danger:focus,
QPushButton#appleDestructiveButton:focus {{
    border-color: {t["text_primary"]};
}}

QPushButton#dangerBtn:disabled,
QPushButton#danger:disabled,
QPushButton#appleDestructiveButton:disabled {{
    background-color: {t["danger_disabled"]};
    color: {t["on_filled_disabled"]};
    border-color: {t["danger_disabled"]};
}}

/* 文字按钮、紧凑按钮与图标按钮：无底无边，悬停才显底色 */
QPushButton#ghostBtn,
QPushButton#appleGhostButton,
QPushButton#compactBtn,
QPushButton#actionBtn,
QPushButton#resetBtn,
QPushButton#fitBtn,
QPushButton#fitThumbBtn,
QPushButton#appleCompactButton,
QPushButton#appleIconButton,
QPushButton#thumbCollapseBtn {{
    background-color: transparent;
    color: {t["text_secondary"]};
    border-color: transparent;
}}

QPushButton#ghostBtn:hover,
QPushButton#appleGhostButton:hover,
QPushButton#compactBtn:hover,
QPushButton#actionBtn:hover,
QPushButton#resetBtn:hover,
QPushButton#fitBtn:hover,
QPushButton#fitThumbBtn:hover,
QPushButton#appleCompactButton:hover,
QPushButton#appleIconButton:hover,
QPushButton#thumbCollapseBtn:hover {{
    background-color: {t["control"]};
    color: {t["text_primary"]};
}}

QPushButton#ghostBtn:pressed,
QPushButton#appleGhostButton:pressed,
QPushButton#compactBtn:pressed,
QPushButton#actionBtn:pressed,
QPushButton#resetBtn:pressed,
QPushButton#fitBtn:pressed,
QPushButton#fitThumbBtn:pressed,
QPushButton#appleCompactButton:pressed,
QPushButton#appleIconButton:pressed,
QPushButton#thumbCollapseBtn:pressed {{
    background-color: {t["well"]};
    color: {t["text_primary"]};
}}

QPushButton#ghostBtn:checked,
QPushButton#appleGhostButton:checked,
QPushButton#compactBtn:checked,
QPushButton#actionBtn:checked,
QPushButton#resetBtn:checked,
QPushButton#fitBtn:checked,
QPushButton#fitThumbBtn:checked,
QPushButton#appleCompactButton:checked,
QPushButton#appleIconButton:checked {{
    background-color: {t["accent_soft"]};
    color: {t["accent"]};
}}

QPushButton#ghostBtn:focus,
QPushButton#appleGhostButton:focus,
QPushButton#compactBtn:focus,
QPushButton#actionBtn:focus,
QPushButton#resetBtn:focus,
QPushButton#fitBtn:focus,
QPushButton#fitThumbBtn:focus,
QPushButton#appleCompactButton:focus,
QPushButton#appleIconButton:focus,
QPushButton#thumbCollapseBtn:focus {{
    border-color: {t["accent"]};
}}

QPushButton#ghostBtn:disabled,
QPushButton#appleGhostButton:disabled,
QPushButton#compactBtn:disabled,
QPushButton#actionBtn:disabled,
QPushButton#resetBtn:disabled,
QPushButton#fitBtn:disabled,
QPushButton#fitThumbBtn:disabled,
QPushButton#appleCompactButton:disabled,
QPushButton#appleIconButton:disabled,
QPushButton#thumbCollapseBtn:disabled {{
    background-color: transparent;
    color: {t["text_disabled"]};
    border-color: transparent;
}}

QPushButton#compactBtn,
QPushButton#actionBtn,
QPushButton#resetBtn,
QPushButton#fitBtn,
QPushButton#fitThumbBtn,
QPushButton#appleCompactButton {{
    border-radius: {RADIUS_SMALL};
    padding: 0 {SPACING_SM};
    min-height: {COMPACT_INNER};
    font-size: {FONT_SIZE_CAPTION};
}}

QPushButton#appleIconButton,
QPushButton#thumbCollapseBtn {{
    border-radius: {RADIUS_SMALL};
    padding: 0px;
    min-height: 0px;
    min-width: 0px;
}}

/* 开关按钮与标签：描边，选中为强调色浅底 */
QPushButton#toggleButton,
QPushButton#chip {{
    background-color: transparent;
    color: {t["text_secondary"]};
    border: 1px solid {t["hairline_solid"]};
    font-weight: 400;
}}

QPushButton#toggleButton {{
    border-radius: {RADIUS_CONTROL};
    padding: 0 {SPACING_MD};
    min-height: {CONTROL_INNER};
}}

QPushButton#chip {{
    border-radius: {COMPACT_PILL};
    padding: 0 {SPACING_MD};
    min-height: {COMPACT_INNER};
}}

QPushButton#toggleButton:hover,
QPushButton#chip:hover {{
    background-color: {t["control"]};
    color: {t["text_primary"]};
}}

QPushButton#toggleButton:pressed,
QPushButton#chip:pressed {{
    background-color: {t["well"]};
    color: {t["text_primary"]};
}}

QPushButton#toggleButton:checked,
QPushButton#chip:checked {{
    background-color: {t["accent_soft"]};
    border-color: {t["accent"]};
    color: {t["accent"]};
    font-weight: 600;
}}

QPushButton#toggleButton:focus,
QPushButton#chip:focus {{
    border-color: {t["accent"]};
}}

QPushButton#toggleButton:disabled,
QPushButton#chip:disabled {{
    background-color: transparent;
    color: {t["text_disabled"]};
    border-color: {t["hairline_solid"]};
}}

/* 提示条：警告与信息两种 */
QFrame#noticeBar {{
    border: none;
    border-radius: {RADIUS_CONTROL};
}}

QFrame#noticeBar[kind="warning"] {{
    background-color: {t["warning_surface"]};
}}

QFrame#noticeBar[kind="info"] {{
    background-color: {t["accent_soft"]};
}}

QLabel#noticeBarText {{
    color: {t["text_primary"]};
}}

/* 空状态：居中的图标、一句话与操作；接受拖放的那一种带虚线框 */
QWidget#emptyState[onCanvas="true"] {{
    background-color: {t["canvas"]};
}}

QFrame#emptyStateCard {{
    background-color: transparent;
    border: none;
}}

QFrame#emptyStateCard[dropTarget="true"] {{
    background-color: {t["surface"]};
    border: 1px dashed {t["text_caption"]};
    border-radius: {RADIUS_PANEL};
}}

QFrame#emptyStateCard[dropTarget="true"][dragActive="true"] {{
    background-color: {t["accent_soft"]};
    border-color: {t["accent"]};
}}

QLabel#emptyStateTitle {{
    color: {t["text_primary"]};
    font-size: {FONT_SIZE_TITLE};
    font-weight: 600;
}}

QLabel#emptyStateText {{
    color: {t["text_caption"]};
}}

QDialogButtonBox {{
    background-color: transparent;
    border: none;
    spacing: 8px;
}}

QLineEdit,
QSpinBox,
QDoubleSpinBox,
QComboBox,
QTextEdit,
QListWidget,
QTableWidget {{
    background-color: {t["page"]};
    color: {t["text_primary"]};
    border: 1px solid {t["hairline"]};
    border-radius: {RADIUS_CONTROL};
    padding: {SPACING_XS} {SPACING_MD};
}}

QLineEdit:focus,
QSpinBox:focus,
QDoubleSpinBox:focus,
QComboBox:focus,
QTextEdit:focus {{
    border: 1px solid {t["accent"]};
}}

QLabel#parameterLabel {{
    color: {t["text_secondary"]};
}}

QLabel#parameterLabel[modified="true"] {{
    color: {t["text_primary"]};
}}

QLabel#parameterLabel:disabled {{
    color: {t["text_disabled"]};
}}

QLineEdit#sliderValueEdit[modified="true"] {{
    color: {t["accent"]};
    font-weight: 600;
}}

QLineEdit#sliderValueEdit:disabled {{
    color: {t["text_disabled"]};
}}

QLineEdit#sliderValueEdit {{
    background-color: transparent;
    color: {t["text_secondary"]};
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    padding: 1px 3px;
    min-height: 18px;
    font-size: {FONT_SIZE_BODY};
}}

QLineEdit#sliderValueEdit:hover {{
    border: 1px solid {t["accent"]};
    background-color: {t["control"]};
}}

QLineEdit#sliderValueEdit:focus {{
    border: 1px solid {t["accent"]};
    background-color: {t["page"]};
    color: {t["text_primary"]};
}}

QLineEdit,
QSpinBox,
QDoubleSpinBox,
QComboBox {{
    min-height: {INPUT_INNER};
}}

QComboBox:hover {{
    background-color: {t["control"]};
    border-color: {t["accent"]};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: {COMBO_DROPDOWN_WIDTH};
    border: none;
    background-color: transparent;
}}

QComboBox::drop-down:hover {{
    background-color: transparent;
}}

QComboBox::down-arrow {{
    image: none;
    border: 4px solid transparent;
    border-top-color: {t["text_caption"]};
    margin-top: 3px;
}}

QComboBox QAbstractItemView {{
    background-color: {t["surface"]};
    color: {t["text_primary"]};
    selection-background-color: {t["accent"]};
    selection-color: {t["on_accent"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_PANEL};
    padding: 4px;
    outline: 0px;
}}

QComboBox QAbstractItemView::item {{
    min-height: 26px;
    padding: 4px 10px;
    border-radius: {RADIUS_SMALL};
    margin: 1px 3px;
}}

QComboBox QAbstractItemView::item:hover {{
    background-color: {t["control"]};
}}

QComboBox QAbstractItemView::item:selected {{
    background-color: {t["accent"]};
    color: {t["on_accent"]};
}}

QSpinBox::up-button,
QSpinBox::down-button,
QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 0px;
}}

QSpinBox::up-arrow,
QSpinBox::down-arrow,
QDoubleSpinBox::up-arrow,
QDoubleSpinBox::down-arrow {{
    width: 0px;
    height: 0px;
    image: none;
}}

QHeaderView::section {{
    background-color: {t["surface"]};
    color: {t["text_primary"]};
    border: 1px solid {t["hairline_solid"]};
    padding: {SPACING_SM};
    font-weight: 600;
}}

/* 顶栏：与侧栏同一层底色，下沿一条细线 */
QWidget#topBar {{
    background-color: {t["surface"]};
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QFrame#topBarSeparator {{
    background-color: {t["hairline_solid"]};
    border: none;
}}

QWidget#windowNotices {{
    background-color: {t["surface"]};
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QPushButton::menu-indicator {{
    image: none;
    width: 0px;
}}

QMenu,
QMenu#appleMenu {{
    background-color: {t["surface"]};
    color: {t["text_primary"]};
    border: 1px solid {t["hairline"]};
    border-radius: {RADIUS_PANEL};
    padding: 6px;
}}

QMenu::item,
QMenu#appleMenu::item {{
    padding: 6px {SPACING_MD};
    border-radius: {RADIUS_SMALL};
    margin: 1px 2px;
    font-weight: 400;
}}

QMenu::item:selected,
QMenu#appleMenu::item:selected {{
    background-color: {t["control"]};
    color: {t["text_primary"]};
}}

QMenu::item:disabled,
QMenu#appleMenu::item:disabled {{
    color: {t["text_disabled"]};
}}

QMenu::separator,
QMenu#appleMenu::separator {{
    height: 1px;
    background-color: {t["hairline"]};
    margin: 4px 6px;
}}

QPushButton#appleDropDown {{
    background-color: {t["page"]};
    color: {t["text_primary"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_CONTROL};
    padding: 0 28px 0 {SPACING_MD};
    text-align: left;
    font-weight: 400;
    min-height: {CONTROL_INNER};
}}

QPushButton#appleDropDown:hover,
QPushButton#appleDropDown:pressed {{
    background-color: {t["page"]};
    border-color: {t["text_caption"]};
}}

QPushButton#appleDropDown:focus {{
    border-color: {t["accent"]};
}}

QPushButton#appleDropDown:disabled {{
    background-color: {t["surface"]};
    color: {t["text_disabled"]};
    border-color: {t["hairline_solid"]};
}}

/* 无框的下拉：只显示文字与箭头符号，用于筛选这类行内选择 */
QPushButton#appleDropDown[flat="true"] {{
    background-color: transparent;
    border-color: transparent;
    border-radius: {RADIUS_SMALL};
    padding: 0 28px 0 {SPACING_SM};
    font-weight: 600;
    min-height: {COMPACT_INNER};
}}

QPushButton#appleDropDown[flat="true"]:hover,
QPushButton#appleDropDown[flat="true"]:pressed {{
    background-color: {t["control"]};
    border-color: transparent;
}}

QPushButton#appleDropDown[flat="true"]:focus {{
    border-color: {t["accent"]};
}}

QPushButton#appleDropDown[flat="true"]:disabled {{
    background-color: transparent;
    border-color: transparent;
}}

QPushButton#appleDropDown[open="true"] {{
    border-color: {t["accent"]};
}}

QPushButton#appleDropDown[flat="true"][open="true"] {{
    background-color: {t["control"]};
    border-color: transparent;
}}

/* 步进器：数值居中，两侧是减与加 */
QSpinBox#appleStepper {{
    background-color: {t["page"]};
    color: {t["text_primary"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_CONTROL};
    padding: 0 30px;
    font-weight: 600;
    min-height: {CONTROL_INNER};
}}

QSpinBox#appleStepper:hover {{
    border-color: {t["text_caption"]};
}}

QSpinBox#appleStepper:focus {{
    border-color: {t["accent"]};
}}

QSpinBox#appleStepper:disabled {{
    background-color: {t["surface"]};
    color: {t["text_disabled"]};
}}

QPushButton#stepperButton {{
    background-color: transparent;
    border: none;
    border-radius: {RADIUS_SMALL};
    padding: 0px;
    min-height: 0px;
    min-width: 0px;
}}

QPushButton#stepperButton:hover {{
    background-color: {t["control"]};
}}

QPushButton#stepperButton:pressed {{
    background-color: {t["well"]};
}}

QPushButton#stepperButton:disabled {{
    background-color: transparent;
}}

QLabel#toggleDescription {{
    color: {t["text_caption"]};
    font-size: {FONT_SIZE_CAPTION};
}}

QFrame#toolSidebarFooter {{
    background-color: transparent;
    border: none;
    border-top: 1px solid {t["hairline_solid"]};
}}

QFrame#toggleSection {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QLabel#stepTitle {{
    color: {t["text_secondary"]};
    font-size: {FONT_SIZE_CAPTION};
    font-weight: 600;
}}

QToolBar {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {t["hairline_solid"]};
    margin: 0px;
    padding: 4px 12px;
    spacing: 6px;
}}

QToolBar::separator {{
    background-color: {t["hairline_solid"]};
    width: 1px;
    height: 16px;
    margin: 5px 6px;
}}

/* 工具栏：编辑工作区最左侧的一列入口，与顶栏同一层底色，右沿一条细线 */
QFrame#toolRail {{
    background-color: {t["surface"]};
    border-right: 1px solid {t["hairline_solid"]};
}}

QToolButton#toolRailButton {{
    background-color: transparent;
    color: {t["text_secondary"]};
    border: 1px solid transparent;
    border-radius: {RADIUS_CONTROL};
    font-size: {FONT_SIZE_CAPTION};
    font-weight: 400;
    padding: {SPACING_XS} 0;
}}

QToolButton#toolRailButton:hover {{
    background-color: {t["control"]};
    color: {t["text_primary"]};
}}

QToolButton#toolRailButton:pressed {{
    background-color: {t["well"]};
}}

QToolButton#toolRailButton:checked {{
    background-color: {t["accent_soft"]};
    color: {t["accent"]};
    font-weight: 600;
}}

QToolButton#toolRailButton:focus {{
    border-color: {t["accent"]};
}}

QToolButton#toolRailButton:disabled {{
    background-color: transparent;
    color: {t["text_disabled"]};
}}

/* 编辑工作区的右侧栏：与顶栏同一层底色，左沿一条细线；里面的工具面板容器不再另画底色 */
QFrame#editSidebar {{
    background-color: {t["surface"]};
    border-left: 1px solid {t["hairline_solid"]};
}}

QWidget#toolSidebar,
QWidget#toolSidebarHeader {{
    background-color: transparent;
    border: none;
}}

QLabel#toolSidebarTitle {{
    font-size: {FONT_SIZE_BODY};
    font-weight: 600;
    color: {t["text_primary"]};
}}

QPushButton#toolSidebarCloseBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    color: {t["text_secondary"]};
    font-size: {FONT_SIZE_BODY};
    padding: 2px;
}}

QPushButton#toolSidebarCloseBtn:hover {{
    background-color: {t["control"]};
    color: {t["text_primary"]};
}}

QFrame#toolSidebarSepTop {{
    background-color: {t["hairline_solid"]};
    border: none;
    height: 1px;
}}

QScrollArea#toolSidebarScrollArea,
QScrollArea#toolSidebarScrollArea > QWidget,
QScrollArea#toolSidebarScrollArea::viewport {{
    background-color: transparent;
    border: none;
}}

QStackedWidget#toolSidebarStack,
QStackedWidget#toolSidebarStack > QWidget {{
    background-color: transparent;
    border: none;
}}

/* 分组标题 (CollapsibleCard)：不套卡片，分组之间以细线分隔 */
QFrame#collapsibleCard {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QPushButton#collapsibleHeaderBtn,
QPushButton#collapsibleHeaderBtn:hover,
QPushButton#collapsibleHeaderBtn:pressed {{
    background-color: transparent;
    color: {t["text_primary"]};
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    text-align: left;
    padding: 0px;
    min-height: 0px;
    font-weight: 600;
}}

QPushButton#collapsibleHeaderBtn:focus {{
    border-color: {t["accent"]};
}}

QWidget#collapsibleContent {{
    background-color: transparent;
}}


QWidget#managementNavigation {{
    background-color: {t["surface"]};
}}

QWidget#managementDetail,
QWidget#managementContent {{
    background-color: {t["page"]};
}}

QFrame#managementActions,
QFrame#managementFooter {{
    border: none;
    border-top: 1px solid {t["hairline_solid"]};
}}

QFrame#managementInfoRow {{
    border: none;
    border-top: 1px solid {t["hairline_solid"]};
}}

QLabel#managementTitle {{
    font-size: {FONT_SIZE_TITLE};
    font-weight: 600;
}}

QLabel#managementSubheading {{
    font-size: {FONT_SIZE_BODY};
    font-weight: 600;
}}

QLabel#managementCaption,
QLabel#managementGroupTitle,
QLabel#managementValue {{
    font-size: {FONT_SIZE_CAPTION};
    color: {t["text_caption"]};
}}

QLabel#managementGroupTitle {{
    font-weight: 600;
}}

QLabel#managementValue {{
    color: {t["text_secondary"]};
}}

QWidget#managementPage QSplitter::handle {{
    background-color: {t["hairline_solid"]};
}}

QToolButton {{
    color: {t["text_secondary"]};
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_CONTROL};
    padding: 0 {SPACING_SM};
    min-height: {CONTROL_INNER};
    font-weight: 400;
}}

QToolButton:hover {{
    background-color: {t["control"]};
    border-color: transparent;
    color: {t["text_primary"]};
}}

QToolButton:pressed {{
    background-color: {t["well"]};
    border-color: transparent;
    color: {t["text_primary"]};
}}

QToolButton:checked {{
    background-color: {t["accent"]};
    border-color: transparent;
    color: {t["on_accent"]};
}}

QToolButton::menu-indicator {{
    image: none;
    width: 0px;
}}

/* 底部工具条：浮在画布下沿的胶囊，阴影由组件按主题设置 */
QFrame#canvasToolbar {{
    background-color: {t["surface"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {CANVAS_TOOLBAR_HEIGHT // 2}px;
}}

/* 图标按钮的通用规则把最小宽高清零，这里按圆形按钮补回 */
QFrame#canvasToolbar QPushButton#appleIconButton {{
    border-radius: {TOOL_BUTTON_SIZE // 2 - 1}px;
    min-width: {TOOL_BUTTON_SIZE - 2}px;
    min-height: {TOOL_BUTTON_SIZE - 2}px;
}}

/* 蒙版：画布下沿的工具条与蒙版页里的小标题、说明 */
QFrame#maskToolBar {{
    background-color: {t["surface"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_PANEL};
}}

QLabel#maskToolTitle,
QLabel#maskDetailTitle {{
    font-weight: 600;
}}

QLabel#maskDetailTitle {{
    font-size: {FONT_SIZE_TITLE};
}}

QLabel#maskSectionTitle {{
    font-size: {FONT_SIZE_CAPTION};
    font-weight: 600;
    color: {t["text_secondary"]};
}}

QLabel#maskCaption {{
    font-size: {FONT_SIZE_CAPTION};
    color: {t["text_caption"]};
}}

QFrame#canvasToolbarSeparator {{
    background-color: {t["hairline_solid"]};
    border: none;
}}

QLineEdit#canvasZoomReadout {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    color: {t["text_primary"]};
    font-weight: 600;
    padding: 0px;
    min-height: {COMPACT_INNER};
}}

QLineEdit#canvasZoomReadout:hover {{
    background-color: {t["control"]};
}}

QLineEdit#canvasZoomReadout:focus {{
    background-color: {t["page"]};
    border-color: {t["accent"]};
}}

QLineEdit#canvasZoomReadout:disabled {{
    color: {t["text_disabled"]};
}}

/* 胶片条：与顶栏同一层底色，上沿一条细线；位置条就是缩略图下面的细滚动条 */
ThumbnailPanel {{
    background-color: {t["surface"]};
    border-top: 1px solid {t["hairline_solid"]};
}}

/* 通用列表规则带内边距与圆角，这里清零，否则缩略图的可用高度会少一截 */
ThumbnailPanel QListWidget,
ThumbnailPanel QListWidget::viewport {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
    margin: 0px;
    outline: none;
}}

ThumbnailPanel QListWidget::item {{
    background-color: transparent;
    border: 2px solid transparent;
    border-radius: {RADIUS_SMALL};
    padding: 1px;
}}

ThumbnailPanel QListWidget::item:hover {{
    background-color: {t["control"]};
}}

ThumbnailPanel QListWidget::item:selected {{
    background-color: {t["accent_soft"]};
    border-color: {t["accent"]};
}}

QScrollBar#thumbnailHorizontalScrollBar:horizontal {{
    height: 3px;
    margin: 0px;
    border: none;
    border-radius: 1px;
    background: {t["track"]};
}}

QScrollBar#thumbnailHorizontalScrollBar::handle:horizontal {{
    background: {t["text_caption"]};
    min-width: {SCROLLBAR_MIN_LEN};
    border-radius: 1px;
}}

QScrollBar#thumbnailHorizontalScrollBar::handle:horizontal:hover,
QScrollBar#thumbnailHorizontalScrollBar::handle:horizontal:pressed {{
    background: {t["accent"]};
}}

QScrollBar#thumbnailHorizontalScrollBar::add-page:horizontal,
QScrollBar#thumbnailHorizontalScrollBar::sub-page:horizontal {{
    background: transparent;
}}

QLabel#filmstripCurrent {{
    color: {t["text_primary"]};
    font-weight: 600;
}}

QLabel#filmstripTotal {{
    color: {t["text_secondary"]};
}}

/* 照片信息栏：与顶栏同一层底色，左沿一条细线；内部不套卡片，分组之间用细线 */
QFrame#infoSidebar {{
    background-color: {t["surface"]};
    border-left: 1px solid {t["hairline_solid"]};
}}

QFrame#infoSidebar[floating="true"],
QFrame#editSidebar[floating="true"] {{
    border-bottom: 1px solid {t["hairline_solid"]};
}}

QScrollArea#infoSidebarScroll,
QScrollArea#infoSidebarScroll > QWidget#qt_scrollarea_viewport,
QWidget#infoSidebarContent {{
    background-color: transparent;
    border: none;
}}

QLabel#infoSidebarTitle {{
    color: {t["text_primary"]};
    font-size: {FONT_SIZE_TITLE};
    font-weight: 600;
}}

QLabel#photoInfoName,
QLabel#photoInfoQuadValue {{
    color: {t["text_primary"]};
    font-weight: 600;
}}

QLabel#photoInfoTime {{
    color: {t["text_secondary"]};
}}

QLabel#photoInfoGroup,
QLabel#photoInfoQuadLabel {{
    color: {t["text_caption"]};
    font-size: {FONT_SIZE_CAPTION};
}}

QLabel#photoInfoGroup {{
    font-weight: 600;
}}

QLabel#photoInfoFieldName {{
    color: {t["text_caption"]};
}}

QLabel#photoInfoValue {{
    color: {t["text_primary"]};
}}

QTextEdit#photoInfoLongValue,
QTextEdit#photoInfoLongValue:focus {{
    background-color: transparent;
    color: {t["text_primary"]};
    border: none;
    border-radius: 0px;
    padding: 0px;
}}

QFrame#photoInfoRule {{
    background-color: {t["hairline_solid"]};
    border: none;
}}

/* 轻提示：与界面反色的一句话 */
QLabel#toast {{
    background-color: {t["text_primary"]};
    color: {t["surface"]};
    border-radius: {RADIUS_CONTROL};
    padding: {SPACING_SM} {SPACING_MD};
}}

QScrollArea#imageScrollArea,
QScrollArea#imageScrollArea > QWidget#qt_scrollarea_viewport,
QScrollArea#imageScrollArea::viewport,
QScrollArea#studioScrollArea,
QScrollArea#studioScrollArea > QWidget#qt_scrollarea_viewport,
QScrollArea#studioScrollArea::viewport {{
    background-color: {t["canvas"]};
    border: none;
}}

QScrollArea {{
    border: none;
    background-color: {t["surface"]};
}}

QScrollArea::viewport {{
    background-color: {t["surface"]};
    border: none;
}}

QScrollArea > QWidget {{
    background-color: {t["surface"]};
    border: none;
}}

QScrollBar:vertical {{
    background: transparent;
    width: {SCROLLBAR_SIZE};
    margin: 2px;
    border-radius: 3px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: {SCROLLBAR_SIZE};
    margin: 2px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical,
QScrollBar::handle:horizontal {{
    background: {t["track"]};
    border-radius: 3px;
}}

QScrollBar::handle:vertical {{
    min-height: {SCROLLBAR_MIN_LEN};
}}

QScrollBar::handle:horizontal {{
    min-width: {SCROLLBAR_MIN_LEN};
}}

QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {{
    background: {t["track_hover"]};
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0px;
    height: 0px;
}}

/* 进度条：显示文字时文字在条内；不显示文字时为细条，进度与占用共用 */
QProgressBar {{
    border: none;
    border-radius: {RADIUS_SMALL};
    text-align: center;
    background-color: {t["track"]};
    color: {t["text_primary"]};
    min-height: 20px;
}}

QProgressBar::chunk {{
    background-color: {t["accent"]};
    border-radius: {RADIUS_SMALL};
}}

QProgressBar[textVisible="false"] {{
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
}}

QProgressBar[textVisible="false"]::chunk {{
    border-radius: 3px;
}}

/* 悬停提示：与界面反色。Qt 会把边框与水平内边距再算进四周的留白，这里的 2px 实际得到上下 5px、左右 7px */
QToolTip {{
    background-color: {t["text_primary"]};
    color: {t["surface"]};
    border: 1px solid {t["text_primary"]};
    padding: 0px 2px;
    font-size: {FONT_SIZE_CAPTION};
}}

QTabWidget::pane {{
    border: none;
    background-color: transparent;
}}

QTabBar {{
    background-color: {t["well"]};
    qproperty-drawBase: 0;
    border: 1px solid {t["hairline"]};
    border-radius: {RADIUS_CONTROL};
    padding: 2px;
}}

QTabBar::tab {{
    background-color: transparent;
    color: {t["text_caption"]};
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    padding: 5px 16px;
    margin: 1px 2px;
    font-weight: 400;
    font-size: {FONT_SIZE_BODY};
}}

QTabBar::tab:selected {{
    background-color: {t["page"]};
    color: {t["text_primary"]};
    border: 1px solid {t["hairline"]};
    font-weight: 600;
}}

QTabBar::tab:!selected:hover {{
    background-color: {t["control"]};
    color: {t["text_secondary"]};
}}

QSlider::groove:horizontal {{
    border: none;
    height: 4px;
    background: {t["track"]};
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {t["accent"]};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
    background-color: {t["knob"]};
    border: 1px solid {t["knob_border"]};
}}

QSlider::handle:horizontal:hover {{
    border-color: {t["accent"]};
}}

QSlider::handle:horizontal:pressed {{
    background-color: {t["knob_pressed"]};
}}

QPushButton#mixerChannelDot {{
    border: 2px solid transparent;
    border-radius: 14px;
    padding: 0px;
    min-height: 0px;
    min-width: 0px;
}}

QPushButton#mixerChannelDot:hover,
QPushButton#mixerChannelDot:focus {{
    border-color: {t["text_caption"]};
}}

QPushButton#mixerChannelDot:checked {{
    border-color: {t["text_primary"]};
}}
{mixer_dots}
QLabel#mixerChannelTitle {{
    color: {t["text_primary"]};
    font-weight: 600;
}}

QSplitter#mainSplitter::handle {{
    background-color: {t["hairline_solid"]};
    height: 2px;
}}

QSplitter::handle {{
    background-color: {t["hairline_solid"]};
}}

QSplitter::handle:hover {{
    background-color: {t["accent"]};
}}

QRadioButton {{
    color: {t["text_primary"]};
    spacing: 8px;
    font-size: {FONT_SIZE_BODY};
}}

QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 9px;
    border: 2px solid {t["track"]};
    background-color: {t["page"]};
}}

QRadioButton::indicator:hover {{
    border-color: {t["accent"]};
}}

QRadioButton::indicator:checked {{
    border: 2px solid {t["accent"]};
    background-color: {t["accent"]};
}}

QRadioButton::indicator:checked:hover {{
    border-color: {t["accent_hover"]};
    background-color: {t["accent_hover"]};
}}

QRadioButton:disabled {{
    color: {t["text_disabled"]};
}}

QRadioButton::indicator:disabled {{
    border-color: {t["hairline_solid"]};
    background-color: {t["control_disabled"]};
}}

QCheckBox {{
    color: {t["text_primary"]};
    spacing: 8px;
    font-size: {FONT_SIZE_BODY};
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 2px solid {t["track"]};
    background-color: {t["page"]};
}}

QCheckBox::indicator:hover {{
    border-color: {t["accent"]};
}}

QCheckBox::indicator:checked {{
    border-color: {t["accent"]};
    background-color: {t["accent"]};
}}

QCheckBox::indicator:checked:hover {{
    border-color: {t["accent_hover"]};
    background-color: {t["accent_hover"]};
}}

QCheckBox:disabled {{
    color: {t["text_disabled"]};
}}

QCheckBox::indicator:disabled {{
    border-color: {t["hairline_solid"]};
    background-color: {t["control_disabled"]};
}}

QRadioButton {{
    color: {t["text_primary"]};
    spacing: 8px;
    font-size: {FONT_SIZE_BODY};
}}

QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 9px;
    border: 2px solid {t["track"]};
    background-color: {t["page"]};
}}

QRadioButton::indicator:hover {{
    border-color: {t["accent"]};
}}

QRadioButton::indicator:checked {{
    border-color: {t["accent"]};
    background-color: {t["accent"]};
}}

QRadioButton::indicator:checked:hover {{
    border-color: {t["accent_hover"]};
    background-color: {t["accent_hover"]};
}}

QRadioButton:disabled {{
    color: {t["text_disabled"]};
}}

QRadioButton::indicator:disabled {{
    border-color: {t["hairline_solid"]};
    background-color: {t["control_disabled"]};
}}

QLabel#imageViewerLabel {{
    background-color: {t["canvas"]};
}}

QLabel#statusInfo {{
    color: {t["accent"]};
}}

QLabel#statusError {{
    color: {t["danger"]};
}}

QLabel#statusSuccess {{
    color: {t["success"]};
}}

QLabel#statusWarning {{
    color: {t["warning"]};
}}

/* AI 助手面板 */
QWidget#aiAssistantPanel,
QFrame#assistantWorkbench,
QWidget#assistantConversationList,
QScrollArea#assistantContextInspector,
QScrollArea#aiTranscript,
QWidget#aiMessages,
QScrollArea#aiSettings,
QWidget#aiSettingsContent {{
    background: {t["surface"]};
    border: none;
}}

QFrame#aiContext {{
    background: {t["control"]};
    border: none;
    border-radius: {RADIUS_CONTROL};
}}

QLabel#aiContextText {{
    color: {t["text_secondary"]};
    font-size: {FONT_SIZE_CAPTION};
}}

QFrame#aiComposer {{
    background: {t["page"]};
    border: 1px solid {t["track"]};
    border-radius: {RADIUS_PANEL};
}}

QFrame#aiComposer[focused="true"] {{
    border-color: {t["accent"]};
}}

QPlainTextEdit#aiInput {{
    background: {t["page"]};
    border: none;
    padding: 0;
    font-size: {FONT_SIZE_BODY};
}}

QFrame#aiUserMessage {{
    background: {t["accent_soft"]};
    border: none;
    border-radius: {RADIUS_PANEL};
}}

/* 循环调整的自查消息：程序发出的一行说明，不用气泡 */
QFrame#aiLoopMessage {{
    background: transparent;
    border: none;
    border-top: 1px solid {t["hairline_solid"]};
    border-radius: 0;
}}

QFrame#aiLoopMessage QTextBrowser#assistantMessageBody {{
    color: {t["text_caption"]};
    font-size: {FONT_SIZE_CAPTION};
}}

QFrame#aiMessage,
QFrame#aiCandidate {{
    background: transparent;
    border: none;
}}

QFrame#aiMessage QLabel {{
    color: {t["text_secondary"]};
}}

QTextBrowser#assistantMessageBody {{
    background: transparent;
    color: {t["text_primary"]};
    border: none;
    padding: 0;
    font-size: {FONT_SIZE_BODY};
}}

QLabel#assistantMessageStatus {{
    color: {t["text_caption"]};
    font-size: {FONT_SIZE_CAPTION};
}}

QLabel#assistantMessageStatus[kind="error"] {{
    color: {t["danger"]};
}}

QPushButton#assistantSend {{
    background: {t["accent"]};
    border: none;
    border-radius: {COMPACT_PILL};
    padding: 0;
}}

QPushButton#assistantSend:hover {{
    background: {t["accent_hover"]};
}}

QPushButton#assistantSend:disabled {{
    background: {t["control_disabled"]};
}}

QPushButton#assistantModelPicker {{
    background: {t["control"]};
    color: {t["text_secondary"]};
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    font-size: {FONT_SIZE_CAPTION};
    min-height: {COMPACT_INNER};
    padding: 0 {SPACING_SM};
    text-align: left;
}}

QPushButton#assistantModelPicker:hover,
QPushButton#assistantModelPicker:pressed {{
    background: {t["well"]};
    color: {t["text_primary"]};
}}

QPushButton#assistantModelPicker:focus {{
    border-color: {t["accent"]};
}}

QPushButton#assistantModelPicker:disabled {{
    background: {t["control_disabled"]};
    color: {t["text_disabled"]};
}}

QPushButton#assistantMode {{
    background: transparent;
    color: {t["text_secondary"]};
    border: 1px solid transparent;
    border-radius: {RADIUS_SMALL};
    min-height: {COMPACT_INNER};
    padding: 0 {COMBO_DROPDOWN_WIDTH} 0 {SPACING_XS};
    font-size: {FONT_SIZE_CAPTION};
    text-align: left;
}}

QPushButton#assistantMode:hover {{
    background: {t["control"]};
}}

QPushButton#assistantMode:focus {{
    border-color: {t["accent"]};
}}

QFrame#assistantCommandMenu {{
    background: {t["page"]};
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_PANEL};
}}

QListWidget#assistantCommandList {{
    background: transparent;
    border: none;
    padding: 0;
    color: {t["text_primary"]};
    font-size: {FONT_SIZE_BODY};
}}

QListWidget#assistantCommandList::item {{
    padding: 0 {SPACING_SM};
    border-radius: {RADIUS_SMALL};
}}

QListWidget#assistantCommandList::item:selected,
QListWidget#assistantCommandList::item:hover {{
    background: {t["control"]};
    color: {t["text_primary"]};
}}

QListWidget#assistantCommandList::item:disabled {{
    color: {t["text_disabled"]};
}}

QFrame#assistantChanges {{
    background: transparent;
    border: none;
    border-left: 1px solid {t["hairline_solid"]};
    padding-left: {SPACING_SM};
}}

QFrame#assistantChanges QFrame#changeCard {{
    background: transparent;
    border: none;
}}

/* 改动卡片：参数名、原值与新值 */
QFrame#changeCard {{
    background: transparent;
    border: 1px solid {t["hairline_solid"]};
    border-radius: {RADIUS_PANEL};
}}

QLabel#changeCardTitle {{
    font-weight: 600;
}}

QLabel#changeCardSection {{
    color: {t["text_caption"]};
    font-size: {FONT_SIZE_CAPTION};
}}

QLabel#changeCardName {{
    color: {t["text_secondary"]};
}}

QLabel#changeCardBefore {{
    color: {t["text_caption"]};
}}

QLabel#changeCardAfter {{
    color: {t["accent"]};
    font-weight: 600;
}}

/* 编辑栈：固定在工具面板下方，它自己上下各有一条细线 */
QWidget#editStack QFrame#collapsibleCard {{
    border-bottom: none;
}}
"""


# =============================================================================
# Token 契约 (Token Contract)
# =============================================================================
# 所有内置主题必须拥有完全一致的 key 集合，否则切主题时某些控件取不到色值会 KeyError。
# 在此显式校验，任何漏键立刻在 import 时报错，而不是运行时才暴露。
assert frozenset(DARK.keys()) == frozenset(LIGHT.keys()) == frozenset(BEIGE.keys()), (
    "Theme key mismatch: "
    f"only in DARK={sorted(set(DARK) - set(LIGHT) - set(BEIGE))}, "
    f"only in LIGHT={sorted(set(LIGHT) - set(DARK) - set(BEIGE))}, "
    f"only in BEIGE={sorted(set(BEIGE) - set(DARK) - set(LIGHT))}"
)

# 所有主题都必须提供的 token 集合 —— 以 DARK 为权威基准
REQUIRED_TOKENS: frozenset[str] = frozenset(DARK.keys())


# =============================================================================
# Palette × Accent 组合主题系统 (Theme Composition)
# =============================================================================
# 当前主题分两个独立维度：
#   • palette  = "调色板"（dark / light）：背景、文字、边框等基础底色
#   • accent   = "强调色"（blue / purple / green / ...）：主操作色，
#                用于 primary button、selection、progress chunk 等
#
# 通过 ``build_theme(palette, accent)`` 把两者合成完整 token 字典：
#     build_theme("dark", "blue")    # 等价于 DARK
#     build_theme("light", "purple") # 浅色 + 紫色主题
#
# 新增 accent: register_accent("purple", dark_tokens={...}, light_tokens={...})
# 新增 palette: register_palette("high_contrast_dark", tokens={...})
#
# 为什么 accent 需要同时提供 dark 和 light 两个变体？
#   同一个 accent 在不同 palette 下的实际色值通常需要微调：
#   light 主题下 primary button 的 bg 颜色要更深以保证文字对比度，
#   dark 主题下要更亮。强行复用一套色值会丢失设计精度。

# 这些 key 的值跟随 accent 变化（其余 key 都属于 palette）
ACCENT_KEYS: frozenset[str] = frozenset({
    "accent",
    "accent_hover",
    "accent_pressed",
    "accent_disabled",
    "accent_soft",
    "on_accent",
    "on_accent_disabled",
})

# 验证 ACCENT_KEYS ⊆ REQUIRED_TOKENS，防止拼写错误
assert ACCENT_KEYS <= REQUIRED_TOKENS, (
    f"ACCENT_KEYS references unknown tokens: "
    f"{sorted(ACCENT_KEYS - REQUIRED_TOKENS)}"
)

PALETTE_KEYS: frozenset[str] = REQUIRED_TOKENS - ACCENT_KEYS


def _split_palette_accent(theme_dict: dict) -> tuple[dict, dict]:
    """把一个完整 token 字典拆成 (palette_part, accent_part)。"""
    palette = {k: v for k, v in theme_dict.items() if k in PALETTE_KEYS}
    accent = {k: v for k, v in theme_dict.items() if k in ACCENT_KEYS}
    return palette, accent


# 从现有 DARK / LIGHT 拆出 palette 与 blue accent 的两个变体
PALETTE_DARK, _ACCENT_BLUE_DARK = _split_palette_accent(DARK)
PALETTE_LIGHT, _ACCENT_BLUE_LIGHT = _split_palette_accent(LIGHT)

# 验证拆分无损：palette + accent 重组应严格等于原字典
assert {**PALETTE_DARK, **_ACCENT_BLUE_DARK} == DARK, (
    "split/merge mismatch for DARK — ACCENT_KEYS may be incomplete"
)
assert {**PALETTE_LIGHT, **_ACCENT_BLUE_LIGHT} == LIGHT, (
    "split/merge mismatch for LIGHT — ACCENT_KEYS may be incomplete"
)


PALETTES: dict[str, dict] = {
    "dark": PALETTE_DARK,
    "light": PALETTE_LIGHT,
}

# accent 名 → {palette_name: token_dict}，每个 accent 必须同时支持所有 palette
ACCENTS: dict[str, dict[str, dict]] = {
    "blue": {
        "dark": _ACCENT_BLUE_DARK,
        "light": _ACCENT_BLUE_LIGHT,
    },
}


def _validate_token_set(
    tokens: dict, expected: frozenset, label: str
) -> None:
    """校验 ``tokens`` 的键集合严格等于 ``expected``。"""
    missing = expected - tokens.keys()
    if missing:
        raise ValueError(f"{label} missing keys: {sorted(missing)}")
    extra = tokens.keys() - expected
    if extra:
        raise ValueError(f"{label} has unexpected keys: {sorted(extra)}")


def register_palette(name: str, tokens: dict) -> None:
    """注册一个新调色板。``tokens`` 必须严格等于 ``PALETTE_KEYS``。"""
    _validate_token_set(tokens, PALETTE_KEYS, f"palette {name!r}")
    PALETTES[name] = tokens


def register_accent(
    name: str, *, dark_tokens: dict, light_tokens: dict
) -> None:
    """注册一个新强调色，必须同时提供 dark / light 两个变体。

    每个变体的键集合必须严格等于 ``ACCENT_KEYS``。
    """
    _validate_token_set(dark_tokens, ACCENT_KEYS, f"accent {name!r} (dark)")
    _validate_token_set(light_tokens, ACCENT_KEYS, f"accent {name!r} (light)")
    ACCENTS[name] = {"dark": dark_tokens, "light": light_tokens}


def build_theme(palette: str, accent: str) -> dict:
    """合成 ``palette × accent`` → 完整 token 字典。

    >>> build_theme("dark", "blue") == DARK
    True
    >>> set(build_theme("light", "blue").keys()) == REQUIRED_TOKENS
    True
    """
    if palette not in PALETTES:
        raise KeyError(
            f"Unknown palette: {palette!r}. Available: {sorted(PALETTES)}"
        )
    if accent not in ACCENTS:
        raise KeyError(
            f"Unknown accent: {accent!r}. Available: {sorted(ACCENTS)}"
        )
    variants = ACCENTS[accent]
    if palette not in variants:
        raise KeyError(
            f"Accent {accent!r} has no variant for palette {palette!r}. "
            f"Defined variants: {sorted(variants)}"
        )
    return {**PALETTES[palette], **variants[palette]}


# =============================================================================
# 主题注册表 (Theme Registry)
# =============================================================================
# 单一来源：所有主题名 → token 字典的映射。
# 新增主题方式有两种：
#   1. 调用 ``register_theme(name, build_theme(palette, accent))`` 用组合系统
#   2. 调用 ``register_theme(name, custom_tokens)`` 直接注册自定义字典
# 两种方式都会走 token 契约校验 (REQUIRED_TOKENS)，缺键立刻报错。
THEMES: dict[str, dict] = {
    "dark": DARK,
    "light": LIGHT,
    "beige": BEIGE,
}


def register_theme(theme_name: str, tokens: dict) -> None:
    """运行时注册一个新主题。``theme_name`` 应为小写英文标识符。

    校验 ``tokens`` 是否覆盖所有 ``REQUIRED_TOKENS``；缺失直接抛
    ``ValueError`` —— 不允许加载残缺主题以避免运行时 KeyError。
    允许 tokens 包含额外的非必需键（不会报错，仅被 QSS 模板忽略）。
    """
    missing = REQUIRED_TOKENS - tokens.keys()
    if missing:
        raise ValueError(
            f"theme {theme_name!r} missing required keys: {sorted(missing)}"
        )
    THEMES[theme_name] = tokens


def get_theme_tokens_by_name(theme_name: str) -> dict:
    """按主题名获取 token 字典，未注册时回退到 DARK。

    这是所有"主题名 → 字典"查询的统一入口；之前散落在 theme.py /
    theme_utils.py / get_theme_variable 里的四处 if/else 全部走这一函数。
    """
    return THEMES.get(theme_name, DARK)


def get_theme_qss(theme_name: str, extra: str = "", *, canvas_background: str = "theme") -> str:
    """按主题名生成完整 QSS，可选追加额外片段；canvas_background 为固定画布底色的名称。"""
    tokens = get_theme_tokens_by_name(theme_name)
    if canvas_background in CANVAS_BACKGROUNDS:
        tokens = {**tokens, "canvas": CANVAS_BACKGROUNDS[canvas_background]}
    qss = _build_qss(tokens)
    if extra.strip():
        return qss + "\n" + extra
    return qss


# ---------- 向后兼容 shims ----------
def get_dark_qss(extra: str = "") -> str:
    """向后兼容：等价于 ``get_theme_qss('dark', extra)``。"""
    return get_theme_qss("dark", extra)


def get_light_qss(extra: str = "") -> str:
    """向后兼容：等价于 ``get_theme_qss('light', extra)``。"""
    return get_theme_qss("light", extra)


def get_theme_variable(theme_name: str, key: str, fallback: str = "") -> str:
    """按主题名和 key 获取单个变量值，便于代码中动态设置颜色。"""
    return get_theme_tokens_by_name(theme_name).get(key, fallback)
