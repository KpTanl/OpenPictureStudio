from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, QTimer, Signal

from ...utils.app_info import APP_NAME
from ...application.photo_browser import PhotoBrowserService
from .photo_browser_runner import PhotoBrowserRunner
from ..widgets.photo_browser import PhotoBrowserWorkspace


class PhotoBrowserController(QObject):
    image_requested = Signal(str)
    single_requested = Signal()
    empty = Signal()

    def __init__(self, manager, metadata, sources, translate, *, drafts_ready, foreground_busy, parent=None):
        super().__init__(parent)
        self.service = PhotoBrowserService(manager, metadata, is_raw=sources.supports_quick_preview, drafts_ready=drafts_ready)
        self.runner = PhotoBrowserRunner(self.service, self, foreground_busy=foreground_busy,
                                         source_active=lambda: self.view.isVisible())
        self.view = PhotoBrowserWorkspace(self.service, self.runner, translate,
                                         settings=QSettings(APP_NAME, "PhotoBrowser"))
        self._panels = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self.refresh)
        self._refreshing = False
        self._closed = False
        self.view.changed.connect(self.refresh)
        self.view.image_requested.connect(self.image_requested)
        self.view.single_requested.connect(self.single_requested)
        self.runner.changed.connect(self.schedule)
        metadata.marks_changed.connect(self.schedule)
        metadata.rating_changed.connect(self.schedule)
        manager.set_filtered_provider(lambda: self.service.results)

    def add_panel(self, panel):
        if panel not in self._panels:
            self._panels.append(panel)
            panel.set_browser(self.service, self.runner)
            panel.selection_changed.connect(self.project)
            panel.mark_requested.connect(self.view.mark)
            panel.rating_requested.connect(self.view.rate)

    def attach(self, sessions, exports, display_settings):
        self.service.raw_mode, self.service.image_mode = display_settings.raw_mode, display_settings.non_raw_mode
        self.service.attach(sessions, exports)
        self.refresh()

    def schedule(self, *_):
        if not self._closed and not self._timer.isActive():
            self._timer.start()

    def invalidate(self, path=None):
        if self.service.library is not None:
            self.service.library.reset_dynamic()
        if self.service.sessions is not None:
            self.runner.invalidate(path)
        self.service.invalidate(path)
        self.schedule()

    def refresh(self):
        if self._closed or self._refreshing:
            return
        self._refreshing = True
        try:
            self.service.refresh()
            current = self.service.manager.current_path
            if current not in self.service.results:
                paths = self.service.manager.image_list
                position = paths.index(current) if current in paths else -1
                after = set(paths[position + 1:])
                target = next((path for path in self.service.results if path in after), None)
                if target is None and self.service.results:
                    target = self.service.results[-1] if position >= 0 else self.service.results[0]
                if target:
                    self.service.select((target,))
                    self.image_requested.emit(target)
                elif current is not None:
                    self.service.manager.current_path = None
                    self.service.manager.current_index = -1
                    self.empty.emit()
            self.project()
            self.runner.scan_sources()
        finally:
            self._refreshing = False

    def project(self):
        for panel in self._panels:
            panel.project_browser()
        self.view.refresh()

    def current_changed(self, path):
        if path in self.service.results and path not in self.service.selected:
            self.service.select((path,))
        self.view.current_changed(path)
        self.project()

    def replace_image_path(self, old_path, new_path):
        self.runner.invalidate(old_path)
        self.service.refresh()
        version = self.service.manager.list_version
        for panel in self._panels:
            panel.update_image_path(old_path, new_path, version=version)
        self.current_changed(self.service.manager.current_path)
        self.schedule()

    def settings_changed(self, settings):
        self.service.raw_mode, self.service.image_mode = settings.raw_mode, settings.non_raw_mode
        self.invalidate()

    def delivery_changed(self, *_):
        if self.service.library is not None:
            self.service.library.reset_dynamic()
        self.service.invalidate()
        self.schedule()

    def perform(self, operation, values, *, show, single):
        service = self.service
        if operation == "get_browser_context":
            return self.context(offset=values.get("offset", 0), limit=values.get("limit", 50))
        references = values.get("photos", [service.reference(path) for path in service.selected])
        paths = service.resolve(values["scope"], references)
        if operation == "set_photo_marks":
            if not paths:
                raise ValueError("batch.no_targets")
            service.mark(paths, values["mark"])
        elif operation == "rate_photos":
            if not paths:
                raise ValueError("batch.no_targets")
            service.rate(paths, values["rating"])
        elif operation == "filter_photos":
            service.set_filter(values["filters"])
        elif operation == "select_photos":
            service.select(paths)
        elif operation == "compare_photos":
            if len(paths) != 2 and not (self.view.pin_snapshot is not None and len(paths) == 1):
                raise ValueError("browser.choose_two")
            service.select(paths)
            show()
            self.view.compare_paths(paths)
        elif operation == "set_sample_photo":
            if values["action"] == "clear":
                self.view.unpin()
            else:
                if len(paths) != 1:
                    raise ValueError("batch.no_targets")
                if not self.view.pin_photo(paths[0]):
                    raise ValueError(self.view.last_error)
        elif operation == "set_browser_view":
            if values["view"] == "single":
                single()
            else:
                show()
                self.view.show_grid()
        else:
            raise ValueError("AI_TOOL_UNAVAILABLE")
        self.refresh()
        return self.context()

    def context(self, *, offset=0, limit=50):
        service = self.service
        self.runner.request_source_check()
        result = service.query(offset=offset, limit=limit)
        pin = self.view.pin_snapshot
        result["sample"] = ({"photo": service.reference(pin.path), "workflow_hash": pin.workflow_hash,
                             "fixed": True, "runtime_only": True} if pin else None)
        result["comparison"] = self.view.comparison_state()
        return result

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self.view.shutdown()
        self.runner.shutdown()
