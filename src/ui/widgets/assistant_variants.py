from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QScrollArea, QSizePolicy, QVBoxLayout,
                             QWidget)

from resources.styles.theme_variables import (ASSISTANT_VARIANT_HEIGHT, ASSISTANT_VARIANT_SUMMARY_HEIGHT,
                                              SPACING_SM, px)
from ...application.assistant_variants import source_matches, variant_groups
from ..image_viewer import ImageViewer
from .apple_button import AppleGhostButton
from .apple_menu import AppleDropDown
from .apple_slider import AppleSlider
from .change_card import ChangeCard
from .collapsible_card import CollapsibleCard
from .assistant_transcript import change_rows
from .message_dialog import confirm_destructive


class _ComparisonArea(QScrollArea):
    """对比画面：占用展开视图里剩下的高度；处于适应窗口时随尺寸重新适应。"""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        viewer = self.widget()
        if viewer is not None and viewer.is_fit_mode():
            viewer.zoom_fit()


class AssistantVariants(QWidget):
    images_requested = Signal(str, str)
    adopt_requested = Signal(str, str)
    expand_requested = Signal()
    end_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._identity, self._group, self._ticket = "", None, 0
        self._images, self._ids = {}, []
        self._expanded, self._tr = False, None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(px(SPACING_SM))
        header = QHBoxLayout()
        self.groups = AppleDropDown(self)
        self.groups.activated.connect(self._select_group)
        self.expand = AppleGhostButton(parent=self)
        self.expand.clicked.connect(self.expand_requested)
        header.addWidget(self.groups, 1)
        header.addWidget(self.expand)
        root.addLayout(header)
        self.body_scroll = QScrollArea(self)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        content = QWidget(self.body_scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.body_scroll.setWidget(content)
        root.addWidget(self.body_scroll)
        self.status = QLabel(self)
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.end = AppleGhostButton(parent=self)
        self.end.clicked.connect(self._end)
        layout.addWidget(self.end)
        self.directions = QLabel(self)
        self.directions.setTextFormat(Qt.TextFormat.PlainText)
        self.directions.setWordWrap(True)
        layout.addWidget(self.directions)
        self.comparison = QWidget(self)
        comparison = QVBoxLayout(self.comparison)
        comparison.setContentsMargins(0, 0, 0, 0)
        switches = QHBoxLayout()
        self.source = AppleDropDown(self)
        self.source.activated.connect(self._render)
        self.overlay = AppleDropDown(self)
        self.overlay.activated.connect(self._render)
        switches.addWidget(self.source, 1)
        switches.addWidget(self.overlay, 1)
        comparison.addLayout(switches)
        self.opacity = AppleSlider(parent=self.comparison)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(50)
        self.opacity.valueEdited.connect(self._render)
        comparison.addWidget(self.opacity)
        self.scroll = _ComparisonArea(self.comparison)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        # 最小高度留得很小：720 高的窗口里对话、对比与输入框也要放得下
        self.scroll.setMinimumHeight(ASSISTANT_VARIANT_HEIGHT // 4)
        self.scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.viewer = ImageViewer(self.scroll)
        self.scroll.setWidget(self.viewer)
        comparison.addWidget(self.scroll)
        zoom = QHBoxLayout()
        for label, method in (("−", self.viewer.zoom_out), ("+", self.viewer.zoom_in)):
            button = AppleGhostButton(label, self.comparison)
            button.clicked.connect(lambda _checked=False, action=method: action())
            zoom.addWidget(button)
        self.fit = AppleGhostButton(parent=self.comparison)
        self.fit.clicked.connect(self.viewer.zoom_fit)
        zoom.addWidget(self.fit)
        zoom.addStretch()
        comparison.addLayout(zoom)
        self.changes = CollapsibleCard("", self.comparison, is_expanded=False)
        self.cards = {}
        for label in ("A", "B"):
            card = ChangeCard(self.changes)
            self.changes.add_widget(card)
            self.cards[label] = card
        comparison.addWidget(self.changes)
        # 对比画面不放进上面的滚动区：展开视图里它直接占用剩下的高度
        root.addWidget(self.comparison, 1)
        actions = QHBoxLayout()
        self.adopt = {}
        for label in ("A", "B"):
            button = AppleGhostButton(parent=self)
            button.clicked.connect(lambda _checked=False, choice=label: self.adopt_requested.emit(self._group["id"], choice))
            self.adopt[label] = button
            actions.addWidget(button, 1)
        root.addLayout(actions)
        self.hide()

    def set_state(self, state, tr, *, photo=None, expanded=False):
        record = state.get("conversation") or {}
        groups = variant_groups(record) if record.get("head") else []
        identity = record.get("id", "")
        ids = [group["id"] for group in groups]
        old_id = self._group["id"] if self._group and identity == self._identity else ""
        target = old_id if ids == self._ids and old_id in ids else ids[-1] if ids else ""
        self._identity, self._all_groups, self._ids, self._tr, self._expanded = identity, groups, ids, tr, expanded
        self._photo, self._busy = photo, identity in state.get("variant_busy", ())
        self.setVisible(bool(groups))
        if not groups:
            self._group = None
            self._images.clear()
            self.viewer.clear()
            self._ticket += 1
        self.comparison.setVisible(expanded)
        # 状态、结束与方向说明只占摘要的高度；展开视图里对比画面在它下面随可用高度伸展
        self.body_scroll.setFixedHeight(ASSISTANT_VARIANT_SUMMARY_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Expanding if expanded else QSizePolicy.Policy.Preferred)
        self.expand.setText(tr("assistant.variant.compare"))
        self.expand.setVisible(not expanded)
        self.fit.setText(tr("assistant.variant.fit"))
        self.end.setText(tr("assistant.variant.end"))
        self.opacity.setAccessibleName(tr("assistant.variant.opacity"))
        self.changes.set_title(tr("assistant.variant.editor_changes"))
        self.groups.clear()
        for group in groups:
            self.groups.addItem(tr("assistant.variant.group", number=group["number"]), group["id"])
        if target:
            self.groups.setCurrentIndex(ids.index(target))
            self._project(groups[ids.index(target)])

    def _end(self):
        """不采用这一组：两份候选都不能再采用，等待选择的任务一并停止。"""
        if self._tr and confirm_destructive(self, self._tr("assistant.variant.end"), self._tr("assistant.variant.end_confirm"),
                                            self._tr("assistant.variant.end")):
            self.end_requested.emit()

    def _select_group(self, index):
        self._project(self._all_groups[index])

    def _project(self, group):
        changed = self._group is None or self._group["id"] != group["id"]
        self._group = group
        tr = self._tr
        self.status.setText(tr("assistant.variant.status." + group["status"]))
        self.end.setVisible(group["status"] in ("pending", "restored"))
        self.end.setEnabled(not self._busy)
        self.directions.setText("\n".join(label + " · " + group["variants"][label]["description"] for label in ("A", "B")))
        for label, button in self.adopt.items():
            button.setText(tr("assistant.variant.adopt", variant=label))
            button.setEnabled(group["status"] in ("pending", "restored") and not self._busy and source_matches(group["source"], self._photo))
            self.cards[label].set_title(label)
            self.cards[label].set_rows(change_rows(group["variants"][label]["editor_changes"], tr))
        selected, overlay = self.source.currentData() or "A", self.overlay.currentData() or ""
        self.source.clear()
        for key in ("A", "B", "before", "reference"):
            self.source.addItem(tr("assistant.variant.image." + key), key)
        self.source.setCurrentIndex(max(0, self.source.findData(selected)))
        self.overlay.clear()
        for key in ("", "before", "reference"):
            self.overlay.addItem(tr("assistant.variant.overlay." + (key or "none")), key)
        self.overlay.setCurrentIndex(max(0, self.overlay.findData(overlay)))
        if changed:
            self._images.clear()
            self.viewer.clear()
            self._ticket += 1
            self.images_requested.emit(group["id"], str(self._ticket))
        elif group.get("images_missing") and self._images:
            self._images.clear()
            self.viewer.clear()
            self._ticket += 1
            self.viewer.setText(tr("assistant.variant.image_missing"))

    def set_images(self, group_id, ticket, images):
        if not self._group or self._group.get("images_missing") or group_id != self._group["id"] or ticket != str(self._ticket):
            return
        self._images = {key: QImage(item["rgba"], *item["size"], item["size"][0] * 4,
                                    QImage.Format.Format_RGBA8888).copy() for key, item in images.items()}
        self._render()

    def _render(self, *_args):
        image = self._images.get(self.source.currentData())
        if image is None:
            self.viewer.clear()
            if self._tr:
                self.viewer.setText(self._tr("assistant.variant.image_missing"))
            return
        overlay = self._images.get(self.overlay.currentData())
        if overlay is not None:
            result = image.copy()
            painter = QPainter(result)
            painter.setOpacity(self.opacity.value() / 100)
            scaled = overlay.scaled(result.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawImage((result.width() - scaled.width()) // 2, (result.height() - scaled.height()) // 2, scaled)
            painter.end()
            image = result
        self.viewer.set_qimage(image, preserve_zoom=True)
        if self.viewer.is_fit_mode():
            self.viewer.zoom_fit()
