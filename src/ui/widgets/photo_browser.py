from __future__ import annotations

import os

from PySide6.QtCore import QAbstractListModel, QEvent, QItemSelectionModel, QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                             QListView, QScrollArea, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from resources.styles.theme_variables import SPACING_MD, SPACING_SM, px
from ..image_viewer import ImageViewer
from .apple_button import AppleButton
from .apple_menu import AppleDropDown
from .rating_filters import RATING_FILTERS


class PhotoGridModel(QAbstractListModel):
    def __init__(self, service, translate, parent=None):
        super().__init__(parent)
        self.service, self.tr = service, translate
        self.paths = ()
        self.icons = {}
        self.errors = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.paths)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.paths):
            return None
        path = self.paths[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return path
        if role == Qt.ItemDataRole.DecorationRole:
            return self.icons.get(path, QIcon())
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            state = self.service.photo_state(path)
            labels = [os.path.basename(path)]
            rating = self.service.metadata.get_rating(path)
            mark = self.service.metadata.get_mark(path)
            if rating:
                labels.append("★" * rating)
            if mark != "unmarked":
                labels.append(self.tr("browser.mark." + mark))
            if state["edited"]:
                labels.append(self.tr("browser.edited.edited"))
            labels.append(self.tr("browser.delivery." + state["delivery"]))
            if state.get("error"):
                labels.append(self.tr(state["error"]))
            if path in self.errors:
                labels.append(self.tr(self.errors[path]))
            elif path not in self.icons:
                labels.append(self.tr("browser.preparing"))
            return "\n".join(labels) if role == Qt.ItemDataRole.ToolTipRole else labels[0] + "\n" + " · ".join(labels[1:])
        return None

    def refresh(self):
        paths = self.service.results
        if paths != self.paths:
            self.beginResetModel()
            self.paths = paths
            self.icons = {}
            self.errors = {}
            self.endResetModel()
        elif paths:
            self.dataChanged.emit(self.index(0), self.index(len(paths) - 1))


class PhotoBrowserWorkspace(QWidget):
    image_requested = Signal(str)
    single_requested = Signal()
    changed = Signal()

    def __init__(self, service, runner, translate, parent=None, *, settings=None):
        super().__init__(parent)
        self.service, self.runner, self.tr = service, runner, translate
        self._pin = None
        self.last_error = ""
        self._targets = [None, None]
        self._compare_pin = False
        self._maximum = [1600, 1600]
        self._shown = [None, None]
        self._syncing = False
        self._projecting = False
        self._settings = settings
        self._buttons = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(*([px(SPACING_MD)] * 4))
        root.setSpacing(px(SPACING_SM))
        actions = QGridLayout()
        for index, (name, action) in enumerate((("single", self.single_requested.emit), ("grid", self.show_grid),
                ("compare", self.compare_selected), ("pin", self.pin_current), ("unpin", self.unpin),
                ("keep", lambda: self.mark("keep")), ("reject", lambda: self.mark("reject")),
                ("unmarked", lambda: self.mark("unmarked")), ("retry", self.runner.retry))):
            button = AppleButton(parent=self)
            button.clicked.connect(action)
            self._buttons[name] = button
            actions.addWidget(button, index // 5, index % 5)
        root.addLayout(actions)
        filters = QGridLayout()
        self._filters = {}
        choices = {"rating": tuple((mode, key) for mode, key, _ in RATING_FILTERS),
                   "mark": tuple((value, "browser.mark." + value) for value in ("all", "keep", "reject", "unmarked")),
                   "kind": tuple((value, "browser.kind." + value) for value in ("all", "raw", "image")),
                   "edited": tuple((value, "browser.edited." + value) for value in ("all", "edited", "unedited")),
                   "delivery": tuple((value, "browser.delivery." + value) for value in ("all", "recorded", "unrecorded", "modified", "unknown"))}
        self._choices = choices
        self._filter_labels = {}
        for index, (name, values) in enumerate(choices.items()):
            label = QLabel(self)
            control = AppleDropDown(self)
            for value, _ in values:
                control.addItem("", value)
            control.activated.connect(lambda _index, key=name, widget=control: self.set_filters({key: widget.currentData()}))
            self._filters[name] = control
            self._filter_labels[name] = label
            filters.addWidget(label, (index // 3) * 2, index % 3)
            filters.addWidget(control, (index // 3) * 2 + 1, index % 3)
        root.addLayout(filters)
        preferences = QHBoxLayout()
        self.advance = QCheckBox(self)
        self.link = QCheckBox(self)
        self.advance.setChecked(settings.value("advance", False, type=bool) if settings is not None else False)
        self.link.setChecked(settings.value("link", True, type=bool) if settings is not None else True)
        self.advance.clicked[bool].connect(lambda value: self._save_preference("advance", value))
        self.link.clicked[bool].connect(lambda value: self._save_preference("link", value))
        preferences.addWidget(self.advance)
        preferences.addWidget(self.link)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        preferences.addWidget(self._status, 1)
        root.addLayout(preferences)
        self.body = QStackedWidget(self)
        root.addWidget(self.body, 1)
        self.grid = QListView(self)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(180, 150))
        self.grid.setGridSize(QSize(210, 250))
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.model = PhotoGridModel(service, translate, self)
        self.grid.setModel(self.model)
        self.grid.installEventFilter(self)
        self.grid.selectionModel().selectionChanged.connect(self._selection_changed)
        self.grid.doubleClicked.connect(self._open_image)
        self.grid.clicked.connect(self._focus_image)
        self.grid.verticalScrollBar().valueChanged.connect(self._schedule_images)
        self.body.addWidget(self.grid)
        self.compare = QSplitter(Qt.Orientation.Horizontal, self)
        self.viewers, self.labels, self.scrolls = [], [], []
        for index in range(2):
            pane = QWidget(self.compare)
            layout = QVBoxLayout(pane)
            label = QLabel(pane)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
            scroll = QScrollArea(pane)
            scroll.setWidgetResizable(False)
            scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            viewer = ImageViewer(scroll)
            scroll.setWidget(viewer)
            layout.addWidget(scroll, 1)
            zoom = QHBoxLayout()
            for name, action in (("fit", viewer.zoom_fit), ("100", lambda _checked=False, target=viewer: target.set_zoom_percent(100))):
                button = AppleButton(parent=pane)
                button.clicked.connect(action)
                self._buttons[f"{name}{index}"] = button
                zoom.addWidget(button)
            layout.addLayout(zoom)
            viewer.view_changed.connect(lambda side=index: self._view_changed(side))
            scroll.horizontalScrollBar().valueChanged.connect(lambda _value, side=index: self._view_changed(side))
            scroll.verticalScrollBar().valueChanged.connect(lambda _value, side=index: self._view_changed(side))
            self.compare.addWidget(pane)
            self.viewers.append(viewer)
            self.labels.append(label)
            self.scrolls.append(scroll)
        self.body.addWidget(self.compare)
        self._images_timer = QTimer(self)
        self._images_timer.setSingleShot(True)
        self._images_timer.timeout.connect(self._request_images)
        runner.ready.connect(self._image_ready)
        runner.clearing.connect(self._clear_images)
        self.retranslate()

    @property
    def pin_snapshot(self):
        return self._pin

    @property
    def preferred_grid(self):
        return self._settings is not None and self._settings.value("mode", "single") == "grid"

    def comparison_state(self):
        rows = []
        for side, path in enumerate(self._targets):
            if path:
                shown = self._shown[side]
                rows.append({"photo": self.service.reference(path), "name": os.path.basename(path),
                             "fixed": side == 0 and self._compare_pin,
                             "displayed": shown is not None,
                             "full_resolution": shown is not None and (shown.pixmap.width(), shown.pixmap.height()) == shown.size})
        return {"active": self.isVisible() and self.body.currentWidget() is self.compare, "photos": rows}

    def _save_preference(self, name, value):
        if self._settings is not None:
            self._settings.setValue(name, value)

    def set_filters(self, values):
        try:
            self.service.set_filter(values)
            self.runner.scan_sources()
            self.changed.emit()
        except (TypeError, ValueError) as error:
            self._status.setText(self.tr(str(error)))

    def refresh(self):
        self._projecting = True
        try:
            self.model.refresh()
            selection = self.grid.selectionModel()
            selection.clearSelection()
            chosen = {self.service.key(path) for path in self.service.selected}
            for row, path in enumerate(self.model.paths):
                if self.service.key(path) in chosen:
                    selection.select(self.model.index(row), QItemSelectionModel.SelectionFlag.Select)
            for name, control in self._filters.items():
                control.setCurrentIndex(control.findData(getattr(self.service.filters, name)))
        finally:
            self._projecting = False
        self._status.setText(self.tr("browser.count", total=len(self.service.results), selected=len(self.service.selected)))
        self._buttons["pin"].setEnabled(self.service.sessions is not None and self.service.manager.current_path is not None)
        self._buttons["unpin"].setEnabled(self._pin is not None)
        self._schedule_images()

    def _selection_changed(self):
        if not self._projecting:
            self.service.select([self.model.paths[index.row()] for index in self.grid.selectionModel().selectedIndexes()])
            self.changed.emit()

    def _open_image(self, index):
        self.image_requested.emit(self.model.paths[index.row()])
        self.single_requested.emit()

    def _focus_image(self, index):
        from PySide6.QtWidgets import QApplication
        if not QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self.image_requested.emit(self.model.paths[index.row()])

    def mark(self, value):
        paths = self.service.selected or ((self.service.manager.current_path,) if self.service.manager.current_path else ())
        before = self.service.results
        current = paths[0] if len(paths) == 1 else self.service.manager.current_path
        following = before[before.index(current) + 1:] if current in before else before
        if self.service.metadata_command is not None:
            def saved(_value, error):
                if error:
                    self._status.setText(self.tr(error))
                    return
                self.service.refresh()
                if self.advance.isChecked() and len(paths) == 1:
                    target = next((path for path in following if path in self.service.results), None)
                    if target:
                        self.service.select((target,))
                        self.image_requested.emit(target)
                self.changed.emit()
            try:
                self.service.metadata_command(paths, {"mark": value}, saved)
            except ValueError as error:
                self._status.setText(self.tr(str(error)))
            return
        try:
            self.service.mark(paths, value)
            if self.advance.isChecked() and len(paths) == 1:
                target = next((path for path in following if path in self.service.results), None)
                if target:
                    self.service.select((target,))
                    self.image_requested.emit(target)
            self.changed.emit()
        except ValueError as error:
            self._status.setText(self.tr(str(error)))

    def rate(self, value):
        paths = self.service.selected or ((self.service.manager.current_path,) if self.service.manager.current_path else ())
        if self.service.metadata_command is not None:
            def saved(_value, error):
                if error:
                    self._status.setText(self.tr(error))
                else:
                    self.service.refresh()
                    self.changed.emit()
            try:
                self.service.metadata_command(paths, {"rating": value}, saved)
            except ValueError as error:
                self._status.setText(self.tr(str(error)))
            return
        try:
            self.service.rate(paths, value)
            self.changed.emit()
        except ValueError as error:
            self._status.setText(self.tr(str(error)))

    def show_grid(self):
        self._clear_compare()
        self.body.setCurrentWidget(self.grid)
        self._save_preference("mode", "grid")
        self._schedule_images()

    def show_single(self):
        self._save_preference("mode", "single")

    def compare_selected(self):
        self.compare_paths((self.service.manager.current_path,) if self._pin is not None and self.service.manager.current_path
                           else self.service.selected)

    def compare_paths(self, paths):
        if len(paths) == 2:
            targets, fixed = list(paths), False
        elif self._pin is not None:
            target = paths[0] if len(paths) == 1 else self.service.manager.current_path
            if target is None:
                return
            targets, fixed = [self._pin.path, target], True
        else:
            self._status.setText(self.tr("browser.choose_two"))
            return
        self.model.icons.clear()
        self.model.errors.clear()
        self._clear_compare()
        self._targets, self._compare_pin = targets, fixed
        self._maximum = [1600, 1600]
        for side, path in enumerate(self._targets):
            self.labels[side].setText(os.path.basename(path) + " · " + self.tr("browser.preparing"))
        self.body.setCurrentWidget(self.compare)
        self.runner.forget("grid")
        self._schedule_images()

    def pin_current(self):
        self.pin_photo(self.service.manager.current_path)

    def pin_photo(self, path):
        if path is None or self.service.sessions is None:
            self.last_error = "batch.no_targets"
            return False
        try:
            graph, source_hash, _ = self.service.workflow(path)
            pin = self.service.sessions.core.capture_export_workflow(path, graph, source_hash=source_hash)
            self.unpin()
            self._pin = pin
            self._buttons["pin"].setText(self.tr("browser.update_pin"))
            self.compare_paths((self.service.manager.current_path or path,))
            self.last_error = ""
            return True
        except Exception as error:
            self.last_error = str(error) if isinstance(error, ValueError) else "error.preview.failed"
            self._status.setText(self.tr(self.last_error))
            return False

    def unpin(self):
        self._clear_compare()
        if self._pin is not None:
            self.runner.release_fixed(self._pin)
            self._pin = None
        if self._compare_pin:
            self._targets = [None, None]
            self._compare_pin = False
            self.body.setCurrentWidget(self.grid)
            self._schedule_images()
        self._buttons["pin"].setText(self.tr("browser.pin"))

    def current_changed(self, path):
        if self.body.currentWidget() is self.compare and self._pin is not None and self._compare_pin:
            self._targets[1] = path
            self.viewers[1].clear()
            self._shown[1] = None
            self.runner.forget("compare1")
            self._maximum[1] = 1600
            self.labels[1].setText(os.path.basename(path) + " · " + self.tr("browser.preparing") if path else "")
            self._schedule_images()

    def _schedule_images(self, *_):
        if self.isVisible():
            self._images_timer.start(0)

    def _request_images(self):
        if not self.isVisible():
            return
        if self.body.currentWidget() is self.grid:
            cell = self.grid.gridSize()
            columns = max(1, self.grid.viewport().width() // max(1, cell.width()))
            start = max(0, self.grid.verticalScrollBar().value() // max(1, cell.height()) * columns - columns)
            count = columns * (self.grid.viewport().height() // max(1, cell.height()) + 3)
            paths = self.model.paths[start:start + count]
            self.model.icons = {path: icon for path, icon in self.model.icons.items() if path in paths}
            for path in paths:
                if self.runner.image(path) is None:
                    self.model.icons.pop(path, None)
            self.runner.request("grid", paths)
            for path in paths:
                self._image_ready(path)
        else:
            for side, path in enumerate(self._targets):
                if path:
                    if side == 0 and self._pin is not None and self._compare_pin:
                        self.runner.request_fixed("compare0", self._pin, self._maximum[side])
                    else:
                        self.runner.request("compare" + str(side), (path,), self._maximum[side])
                    self._image_ready(path)

    def _image_ready(self, path):
        if not self.isVisible():
            return
        if self.body.currentWidget() is self.grid and path in self.model.paths:
            image = self.runner.image(path)
            if image is not None:
                if image.pixmap is not None:
                    self.model.icons[path] = QIcon(image.pixmap)
                    self.model.errors.pop(path, None)
                if image.error:
                    self.model.errors[path] = image.error
                row = self.model.paths.index(path)
                self.model.dataChanged.emit(self.model.index(row), self.model.index(row))
                if image.error:
                    self._status.setText(self.tr(image.error))
        if self.body.currentWidget() is self.compare:
            for side, target in enumerate(self._targets):
                if path != target:
                    continue
                image = self.runner.image(path, self._maximum[side], fixed=self._pin if side == 0 and self._compare_pin else None)
                if image is None or self._shown[side] is image:
                    continue
                if image.error:
                    self.labels[side].setText(os.path.basename(path) + " · " + self.tr(image.error))
                    continue
                state = self.viewers[side].view_state()
                self._syncing = True
                try:
                    self.viewers[side].set_pixmap(image.pixmap, reference_size=QSize(*image.size))
                    self.viewers[side].restore_view_state(state)
                    self._shown[side] = image
                    self.runner.retain("compare" + str(side), image)
                    self.labels[side].setText(os.path.basename(path) + (" · " + self.tr("browser.fixed") if side == 0 and self._compare_pin else ""))
                finally:
                    self._syncing = False

    def _view_changed(self, side):
        if self._syncing or self.body.currentWidget() is not self.compare:
            return
        viewer = self.viewers[side]
        if self._shown[side] is not None and not viewer.is_fit_mode() and self._maximum[side] is not None:
            self._maximum[side] = None
            self._schedule_images()
        if self.link.isChecked():
            self._syncing = True
            try:
                self.viewers[1 - side].restore_view_state(viewer.view_state())
                if not viewer.is_fit_mode() and self._maximum[1 - side] is not None:
                    self._maximum[1 - side] = None
                    self._schedule_images()
            finally:
                self._syncing = False

    def _clear_images(self, path):
        for target in list(self.model.icons):
            if not path or self.service.key(target) == self.service.key(path):
                self.model.icons.pop(target, None)
        for target in list(self.model.errors):
            if not path or self.service.key(target) == self.service.key(path):
                self.model.errors.pop(target, None)
        for side, target in enumerate(self._targets):
            if target and (not path or self.service.key(path) == self.service.key(target)):
                self.viewers[side].clear()
                self._shown[side] = None
                self.runner.retain("compare" + str(side), None)

    def _clear_compare(self):
        for viewer in self.viewers:
            viewer.clear()
        self._shown = [None, None]
        self.runner.forget("compare0")
        self.runner.forget("compare1")

    def retranslate(self):
        for name, button in self._buttons.items():
            key = "fit" if name.startswith("fit") else "100" if name.startswith("100") else name
            button.setText(self.tr("browser." + ("update_pin" if name == "pin" and self._pin else key)))
        for name, control in self._filters.items():
            self._filter_labels[name].setText(self.tr("browser.filter." + name))
            for index, (_, key) in enumerate(self._choices[name]):
                control.setItemText(index, self.tr(key))
        self.advance.setText(self.tr("browser.advance"))
        self.link.setText(self.tr("browser.link"))
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        self._images_timer.stop()
        self.model.icons.clear()
        self._clear_compare()
        self.runner.forget("grid")
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_images()

    def shutdown(self):
        self.unpin()
        self.model.icons.clear()
        for viewer in self.viewers:
            viewer.shutdown()

    def eventFilter(self, watched, event):
        if (watched is self.grid and event.type() == QEvent.Type.KeyPress
                and event.modifiers() == Qt.KeyboardModifier.NoModifier and not event.isAutoRepeat()):
            value = {Qt.Key.Key_P: "keep", Qt.Key.Key_X: "reject", Qt.Key.Key_U: "unmarked"}.get(event.key())
            if value is not None:
                self.mark(value)
                return True
            if Qt.Key.Key_0 <= event.key() <= Qt.Key.Key_5 or event.key() == Qt.Key.Key_QuoteLeft:
                self.rate(0 if event.key() == Qt.Key.Key_QuoteLeft else event.key() - Qt.Key.Key_0)
                return True
        return super().eventFilter(watched, event)
