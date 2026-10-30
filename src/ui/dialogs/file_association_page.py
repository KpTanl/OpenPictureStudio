"""设置窗口里的“文件关联”页：逐个格式开关文件关联，顶部引导到系统的默认应用设置。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_LG, SPACING_SM, px
from ..themed_mixin import ThemedI18nMixin
from ..widgets import AppleButton, AppleCompactButton, AppleSegmentedControl
from ..widgets.message_dialog import show_confirm, show_error, show_info
from ..widgets.setting_row import SettingRow, SettingSection
from ..widgets.toggle_switch import ToggleSwitch
from ...core.constants import RAW_EXTENSIONS, SUPPORTED_EXTENSIONS
from ...utils import file_association as fa


# 支持格式的元数据定义（中、英、法文描述与分类）
FORMAT_METADATA: dict[str, dict[str, str]] = {
    ".jpg": {"zh": "JPEG 标准图像", "en": "JPEG Standard Image", "fr": "Image JPEG standard", "cat": "common"},
    ".jpeg": {"zh": "JPEG 标准图像", "en": "JPEG Standard Image", "fr": "Image JPEG standard", "cat": "common"},
    ".jpe": {"zh": "JPEG 图像", "en": "JPEG Image", "fr": "Image JPEG", "cat": "common"},
    ".png": {"zh": "PNG 无损便携图像", "en": "PNG Lossless Image", "fr": "Image PNG sans perte", "cat": "common"},
    ".bmp": {"zh": "Windows 标准位图", "en": "Windows Bitmap Image", "fr": "Image bitmap Windows", "cat": "common"},
    ".gif": {"zh": "GIF 动态图 / 静态图", "en": "GIF Animation / Image", "fr": "Animation / image GIF", "cat": "common"},
    ".webp": {"zh": "WebP 现代网页图像", "en": "WebP Modern Image", "fr": "Image WebP moderne", "cat": "common"},
    ".tiff": {"zh": "TIFF 高保真图像", "en": "TIFF High-Fidelity Image", "fr": "Image TIFF haute fidélité", "cat": "common"},
    ".tif": {"zh": "TIFF 高保真图像", "en": "TIFF High-Fidelity Image", "fr": "Image TIFF haute fidélité", "cat": "common"},
    ".ico": {"zh": "Windows 图标文件", "en": "Windows Icon File", "fr": "Fichier d’icône Windows", "cat": "common"},
    ".ppm": {"zh": "便携式像素图 (PPM)", "en": "Portable Pixmap (PPM)", "fr": "Pixmap portable (PPM)", "cat": "common"},
    ".pgm": {"zh": "便携式灰度图 (PGM)", "en": "Portable Graymap (PGM)", "fr": "Graymap portable (PGM)", "cat": "common"},
    ".pbm": {"zh": "便携式位图 (PBM)", "en": "Portable Bitmap (PBM)", "fr": "Bitmap portable (PBM)", "cat": "common"},
    # RAW 格式
    ".cr2": {"zh": "佳能 RAW 格式 (CR2)", "en": "Canon RAW Image (CR2)", "fr": "Image RAW Canon (CR2)", "cat": "raw"},
    ".cr3": {"zh": "佳能新一代 RAW (CR3)", "en": "Canon New-Gen RAW (CR3)", "fr": "RAW Canon nouvelle génération (CR3)", "cat": "raw"},
    ".nef": {"zh": "尼康 RAW 格式 (NEF)", "en": "Nikon RAW Image (NEF)", "fr": "Image RAW Nikon (NEF)", "cat": "raw"},
    ".nrw": {"zh": "尼康便携 RAW (NRW)", "en": "Nikon Compact RAW (NRW)", "fr": "RAW Nikon compact (NRW)", "cat": "raw"},
    ".arw": {"zh": "索尼 Alpha RAW (ARW)", "en": "Sony Alpha RAW (ARW)", "fr": "RAW Sony Alpha (ARW)", "cat": "raw"},
    ".raf": {"zh": "富士胶片 RAW (RAF)", "en": "Fujifilm RAW (RAF)", "fr": "RAW Fujifilm (RAF)", "cat": "raw"},
    ".dng": {"zh": "Adobe 通用数字底片 (DNG)", "en": "Adobe Digital Negative (DNG)", "fr": "Négatif numérique Adobe (DNG)", "cat": "raw"},
    ".orf": {"zh": "奥林巴斯 RAW (ORF)", "en": "Olympus RAW (ORF)", "fr": "RAW Olympus (ORF)", "cat": "raw"},
    ".pef": {"zh": "宾得 RAW 格式 (PEF)", "en": "Pentax RAW (PEF)", "fr": "RAW Pentax (PEF)", "cat": "raw"},
    ".rw2": {"zh": "松下 Lumix RAW (RW2)", "en": "Panasonic Lumix RAW (RW2)", "fr": "RAW Panasonic Lumix (RW2)", "cat": "raw"},
}


class FormatItemWidget(SettingRow):
    """一种文件格式一行：名称与扩展名在左，右侧是关联开关；点整行也能切换。"""

    def __init__(self, ext: str, page: FileAssociationPage, parent=None):
        super().__init__(parent)
        self.ext = ext
        self.page = page
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._meta = FORMAT_METADATA.get(
            ext,
            {"zh": f"{ext.upper()} 文件", "en": f"{ext.upper()} File", "fr": f"Fichier {ext.upper()}", "cat": "common"},
        )
        self._category = "raw" if ext in RAW_EXTENSIONS else "common"
        self.switch = ToggleSwitch()
        self.switch.toggled.connect(self._on_switch_toggled)
        self.add_control(self.switch)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.switch.toggle()
            event.accept()
        else:
            super().mousePressEvent(event)

    @property
    def category(self) -> str:
        return self._category

    def matches_search(self, text: str) -> bool:
        """检查当前项目是否匹配搜索词。"""
        if not text:
            return True
        query = text.lower().strip()
        names = (self._meta.get(lang, "").lower() for lang in ("zh", "en", "fr"))
        return query in self.ext.lower() or query in self._category or any(query in name for name in names)

    def _on_switch_toggled(self, checked: bool) -> None:
        if checked:
            fa.register_extension(self.ext)
        else:
            fa.unregister_extension(self.ext)

    def retranslate_ui(self) -> None:
        cur_lang = self.page._current_language().split("-")[0].lower()
        format_name = self._meta.get(cur_lang, self._meta.get("en", self.ext))
        cat_text = (
            self.page._tr("file_assoc.category_raw", "Camera RAW")
            if self._category == "raw"
            else self.page._tr("file_assoc.category_common", "Common")
        )

        self.set_text(format_name, f"{self.ext}  ·  {cat_text}")


class FileAssociationPage(QWidget, ThemedI18nMixin):
    """逐个格式开关文件关联，开关即生效；双击默认打开要到系统设置里确认，由顶部一行引导过去。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item_widgets: dict[str, FormatItemWidget] = {}
        self._current_filter = "all"
        self._init_themed_i18n()
        # 销毁时退订翻译服务；回调不能是本对象的方法，销毁时它已经不会被调用
        service, callback = self.translation_service, self._on_language_changed
        if service:
            self.destroyed.connect(lambda: service.remove_observer(callback))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_LG))
        if fa.is_windows():
            self._setup_ui(layout)
            self._refresh_status()
        else:
            self.notice = QLabel(self)
            self.notice.setObjectName("settingRowDescription")
            self.notice.setWordWrap(True)
            layout.addWidget(self.notice)
        self.retranslate_ui()

    def _current_language(self) -> str:
        return self.translation_service.get_language() if self.translation_service else "zh-CN"

    def _setup_ui(self, layout: QVBoxLayout) -> None:
        self.desc = QLabel(self)
        self.desc.setObjectName("settingRowDescription")
        self.desc.setWordWrap(True)
        layout.addWidget(self.desc)

        # 双击默认打开要到系统设置里确认
        self.default_section = SettingSection(self)
        self.tip_card = self.default_section.add_row(SettingRow(self.default_section))
        self.btn_system = AppleButton(parent=self.tip_card)
        self.btn_system.clicked.connect(fa.open_system_default_apps)
        self.tip_card.add_control(self.btn_system)
        layout.addWidget(self.default_section)

        # 搜索一行，分类筛选与批量操作一行，下面一种格式一行
        self.formats_section = SettingSection(self)
        tools = QVBoxLayout()
        tools.setContentsMargins(0, px(SPACING_SM), 0, px(SPACING_SM))
        tools.setSpacing(px(SPACING_SM))
        self.search_input = QLineEdit(self)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_or_filter_changed)
        tools.addWidget(self.search_input)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(px(SPACING_SM))
        self.filter_segment = AppleSegmentedControl(self)
        self.filter_segment.add_segment(self._tr("file_assoc.filter_all", "All"))
        self.filter_segment.add_segment(self._tr("file_assoc.filter_common", "Common"))
        self.filter_segment.add_segment(self._tr("file_assoc.filter_raw", "RAW"))
        self.filter_segment.currentIndexChanged.connect(self._on_filter_tab_clicked)
        toolbar.addWidget(self.filter_segment)
        toolbar.addStretch(1)
        self.btn_enable_all = AppleCompactButton()
        self.btn_enable_all.clicked.connect(self._enable_all)
        toolbar.addWidget(self.btn_enable_all)
        self.btn_disable_all = AppleCompactButton()
        self.btn_disable_all.clicked.connect(self._disable_all)
        toolbar.addWidget(self.btn_disable_all)
        tools.addLayout(toolbar)
        self.formats_section.add_layout(tools)
        for ext in SUPPORTED_EXTENSIONS:
            row = FormatItemWidget(ext, self, self.formats_section)
            self._item_widgets[ext] = row
            self.formats_section.add_row(row)
        self.empty_label = QLabel(self.formats_section)
        self.empty_label.setObjectName("settingRowDescription")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        self.formats_section.add_widget(self.empty_label)
        layout.addWidget(self.formats_section)

    def retranslate_ui(self) -> None:
        if hasattr(self, "notice"):
            self.notice.setText(
                self._tr(
                    "file_assoc.non_windows_notice",
                    "File association is only supported on Windows.",
                )
            )
            return

        self.desc.setText(
            self._tr(
                "file_assoc.desc",
                "Enable to open images with OpenPictureStudio from the context menu. To open on double-click, set as default app in Windows settings.",
            )
        )
        self.tip_card.set_text(
            self._tr("file_assoc.default_title", "Set as Default Photo Viewer"),
            self._tr(
                "file_assoc.default_desc",
                "On Windows 10/11, setting OpenPictureStudio as default for double-click requires confirmation in Windows Default Apps settings.",
            ))
        self.btn_system.setText(self._tr("file_assoc.open_system_settings", "Open Windows Settings ↗"))

        self.formats_section.set_title(self._tr("file_assoc.title", "File Association Management"))
        self.search_input.setPlaceholderText(
            self._tr("file_assoc.search_placeholder", "Search extension or format...")
        )
        self.filter_segment.set_segment_text(0, self._tr("file_assoc.filter_all", "All"))
        self.filter_segment.set_segment_text(1, self._tr("file_assoc.filter_common", "Common"))
        self.filter_segment.set_segment_text(2, self._tr("file_assoc.filter_raw", "RAW"))
        self.btn_enable_all.setText(self._tr("file_assoc.enable_all", "Enable All"))
        self.btn_disable_all.setText(self._tr("file_assoc.disable_all", "Disable All"))
        self.empty_label.setText(self._tr("search.no_results", "No matching formats found"))

        for card in self._item_widgets.values():
            card.retranslate_ui()

    def _refresh_status(self) -> None:
        reg_status = fa.get_registration_status()
        for ext, card in self._item_widgets.items():
            is_associated = reg_status.get(ext, False)
            card.switch.set_checked_silent(is_associated)

    def _on_filter_tab_clicked(self, btn_id: int) -> None:
        if btn_id == 0:
            self._current_filter = "all"
        elif btn_id == 1:
            self._current_filter = "common"
        elif btn_id == 2:
            self._current_filter = "raw"
        self._on_search_or_filter_changed()

    def _on_search_or_filter_changed(self) -> None:
        query = self.search_input.text().strip()
        visible_count = 0

        for card in self._item_widgets.values():
            match_category = (
                self._current_filter == "all"
                or card.category == self._current_filter
            )
            match_query = card.matches_search(query)
            should_show = match_category and match_query
            card.setVisible(should_show)
            if should_show:
                visible_count += 1

        self.empty_label.setVisible(visible_count == 0)

    def _enable_all(self) -> None:
        results = fa.register_file_associations()
        failed = [ext for ext, ok in results.items() if not ok]
        self._refresh_status()

        if failed:
            show_error(
                self,
                self._tr("file_assoc.warning.registration_title", "Association Warning"),
                self._tr(
                    "file_assoc.warning.registration_message",
                    "Failed to associate the following extensions:\n{failed}\n\nTry running the application as administrator.",
                    failed=", ".join(failed),
                ),
            )
        else:
            show_info(
                self,
                self._tr("file_assoc.complete.registration_title", "Association Complete"),
                self._tr(
                    "file_assoc.complete.registration_message",
                    "OpenPictureStudio has been associated with all supported image formats.",
                ),
            )

    def _disable_all(self) -> None:
        reply = show_confirm(
            self,
            self._tr("file_assoc.confirm.unregister_title", "Remove File Associations"),
            self._tr(
                "file_assoc.confirm.unregister_message",
                "Are you sure you want to remove all file associations?\nThis removes OpenPictureStudio from the 'Open With' menu.",
            ),
            yes_text=self._tr("file_assoc.yes", "Yes"),
            no_text=self._tr("file_assoc.no", "No"),
            default_yes=False,
        )
        if reply:
            fa.unregister_file_associations()
            self._refresh_status()
            show_info(
                self,
                self._tr("file_assoc.info.unregistered_title", "Associations Removed"),
                self._tr(
                    "file_assoc.info.unregistered_message",
                    "All file associations have been removed.",
                ),
            )

