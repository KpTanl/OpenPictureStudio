"""壁纸显示样式选择对话框。"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from resources.styles.theme_variables import SPACING_LG, SPACING_MD, SPACING_SM, SPACING_XL, px
from ..base_window import BaseDialog
from ..widgets.apple_button import AppleBrandButton, AppleButton
from ..widgets.grouped_list import GroupedOptionCard
from ...core.contracts.wallpaper import WallpaperPosition


class WallpaperDialog(BaseDialog):
    """设为壁纸对话框：选一种显示方式，右下角是取消与确定。"""

    def __init__(self, image_path: str = "", parent=None):
        self._image_path = image_path

        self._style_configs = [
            ("dialog.wallpaper.style.fill", "Fill (recommended)", WallpaperPosition.FILL),
            ("dialog.wallpaper.style.fit", "Fit", WallpaperPosition.FIT),
            ("dialog.wallpaper.style.stretch", "Stretch", WallpaperPosition.STRETCH),
            ("dialog.wallpaper.style.center", "Center", WallpaperPosition.CENTER),
            ("dialog.wallpaper.style.tile", "Tile", WallpaperPosition.TILE),
        ]

        super().__init__(
            title_key="dialog.wallpaper.title",
            title_default="Set as Wallpaper",
            min_width=320,
            min_height=340,
            default_width=360,
            default_height=365,
            modal=True,
            parent=parent,
        )

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        layout.setSpacing(px(SPACING_MD))

        self._desc_label = QLabel(
            self._tr("dialog.wallpaper.select_style", "Select wallpaper style:")
        )
        layout.addWidget(self._desc_label)

        self._option_card = GroupedOptionCard(self)
        for i, (key, default, _) in enumerate(self._style_configs):
            self._option_card.add_item(
                key,
                self._tr(key, default),
                default_checked=(i == 0),
            )
        self._option_card.item_double_clicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._option_card)

        layout.addStretch()

        # Footer 按钮组
        footer = QHBoxLayout()
        footer.setContentsMargins(0, px(SPACING_SM), 0, 0)
        footer.setSpacing(px(SPACING_SM))
        footer.addStretch()

        self._cancel_btn = AppleButton(self._tr("button.cancel", "Cancel"))
        self._cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self._cancel_btn)

        self._ok_btn = AppleBrandButton(self._tr("button.ok", "OK"))
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_apply)
        footer.addWidget(self._ok_btn)

        layout.addLayout(footer)

    def _on_item_double_clicked(self, idx: int, key: str) -> None:
        self._on_apply()

    def _on_apply(self) -> None:
        idx = self._option_card.checked_index()
        if idx < 0 or idx >= len(self._style_configs):
            self.reject()
            return

        self.accept()

    def selected_position(self) -> WallpaperPosition:
        return self._style_configs[self._option_card.checked_index()][2]

    def retranslate_ui(self) -> None:
        self.setWindowTitle(self._tr(self._title_key, self._title_default))
        self._desc_label.setText(
            self._tr("dialog.wallpaper.select_style", "Select wallpaper style:")
        )
        for i, (key, default, _) in enumerate(self._style_configs):
            self._option_card.set_item_text(i, self._tr(key, default))
        self._cancel_btn.setText(self._tr("button.cancel", "Cancel"))
        self._ok_btn.setText(self._tr("button.ok", "OK"))
