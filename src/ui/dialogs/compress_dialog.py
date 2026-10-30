"""图片格式处理对话框。"""

from __future__ import annotations

from ...application.image_output_service import ImageOutputService, BatchOutputResult
from ...application.output_writer import OperationCancellation
from ..services.background_operation import batch_message

import os
import logging
from functools import partial
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from resources.styles.theme_variables import SPACING_LG, SPACING_SM, SPACING_XL, px
from ..base_window import BaseDialog
from ..widgets.apple_button import AppleBrandButton, AppleButton, AppleCompactButton
from ..widgets.apple_dropdown import AppleDropDown
from ..widgets.parameter_row import ParameterRow, ParameterSpec
from ..widgets.setting_row import SettingRow, SettingSection, plain_label
from ..widgets.usage_bar import UsageBar
from ...core.constants import SUPPORTED_FORMATS_FILTER

_NUMBER_WIDTH = 96
_CHOICE_WIDTH = 160


logger = logging.getLogger(__name__)


class ImageProcessingWorker(QThread):
    progress_updated = Signal(int, int, str)

    def __init__(self, generation, tasks, output_service, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.output_service = output_service
        self.cancellation = OperationCancellation()
        self.result = BatchOutputResult()
        self.tasks = tuple(tasks)

    def run(self):
        jobs = [(task[1], partial(self._process, index, task)) for index, task in enumerate(self.tasks)]
        self.result = self.output_service.batch(jobs, self.cancellation)

    def _process(self, index, task):
        in_path, out_path, quality, options, stamp = task
        self.progress_updated.emit(self.generation, index, os.path.basename(in_path))
        try:
            self.output_service.resize_and_save(in_path, out_path, quality=quality, options=options,
                                               expected_stamp=stamp, cancellation=self.cancellation)
        except InterruptedError:
            raise
        except Exception:
            logger.exception("Failed to process image %s -> %s", in_path, out_path)
            raise

    def stop(self):
        self.cancellation.cancel()


class SourceInfoWorker(QThread):
    source_ready = Signal(int, str, object, str)

    def __init__(self, generation, paths, output_service, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.paths = tuple(paths)
        self.output_service = output_service
        self.cancellation = OperationCancellation()

    def run(self):
        for path in self.paths:
            if self.cancellation.is_cancelled():
                break
            try:
                info = self.output_service.inspect_source(path, cancellation=self.cancellation)
            except InterruptedError:
                break
            except FileNotFoundError:
                self.source_ready.emit(self.generation, path, None, "compress.info.not_found")
            except Exception as error:
                logger.exception("Failed to read image metadata: %s", path)
                self.source_ready.emit(self.generation, path, None, str(error))
            else:
                self.source_ready.emit(self.generation, path, info, "")


class QualityMatchWorker(QThread):
    def __init__(self, generation, path, options, target, stamp, output_service, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.path = path
        self.options = options
        self.target = target
        self.stamp = stamp
        self.output_service = output_service
        self.cancellation = OperationCancellation()
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.output_service.match_jpeg_quality(
                self.path, options=self.options, target_bytes=self.target,
                expected_stamp=self.stamp, cancellation=self.cancellation)
        except InterruptedError as error:
            self.error = error
        except Exception as error:
            logger.exception("Failed to match image quality: %s", self.path)
            self.error = error


class ImageProcessingDialog(BaseDialog):
    def __init__(self, initial_path=None, parent=None, output_service=None, *,
                 initial_paths=None, initial_format="jpg", initial_quality=85):
        self._output_service = output_service or ImageOutputService()
        format_info = self._output_service.output_format(initial_format)
        self._initial_quality = self._output_service.validate_quality(initial_quality)
        paths = tuple(dict.fromkeys(initial_paths if initial_paths is not None else
                                    ([initial_path] if initial_path else [])))
        self.initial_path = paths[0] if paths else None
        self._batch_files = list(paths) if len(paths) > 1 else []
        self._worker = None
        self._info_worker = None
        self._match_worker = None
        self._source_info = {}
        self._source_errors = {}
        self._info_generation = 0
        self._match_generation = 0
        self._export_generation = 0
        self._pending_info = None
        self._pending_match = None
        self._closing = False
        self._task_count = 0
        self._status_key = "compress.status.ready"
        self._status_values = {}
        self._status_kind = "info"

        super().__init__(
            min_width=700,
            min_height=600,
            default_width=850,
            default_height=700,
            modal=True,
            parent=parent,
        )

        self._init_ui()
        for path in self._batch_files:
            self.list_widget.addItem(QListWidgetItem(path))
        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(200)
        self._info_timer.timeout.connect(self._queue_source_info)
        self.format_combo.setCurrentIndex(self.format_combo.findData(format_info.extension))
        self.retranslate_ui()

        # 如果从主窗口带了路径过来，自动加载
        if self.initial_path:
            self.input_edit.setText(self.initial_path)

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(px(SPACING_XL), px(SPACING_XL), px(SPACING_XL), px(SPACING_LG))
        root_layout.setSpacing(px(SPACING_LG))

        # 来源：路径、浏览，下面一句是这张图的尺寸与格式
        self.source_group = SettingSection(self)
        self.input_edit = QLineEdit()
        self.input_edit.textChanged.connect(self._on_input_changed)
        self.source_row = self.source_group.add_row(SettingRow(self.source_group, leading=self.input_edit))
        self.btn_browse = AppleCompactButton()
        self.btn_browse.clicked.connect(self._browse_input)
        self.source_row.add_control(self.btn_browse)
        root_layout.addWidget(self.source_group)

        columns = QHBoxLayout()
        columns.setSpacing(px(SPACING_XL))

        # 输出设置：格式、质量、目标大小
        self.quality_group = SettingSection(self)
        self.format_row = self.quality_group.add_row(SettingRow(self.quality_group))
        self.format_combo = AppleDropDown()
        self.format_combo.setFixedWidth(_CHOICE_WIDTH)
        for format_info in self._output_service.output_formats:
            self.format_combo.addItem(format_info.label, format_info.extension)
        self.format_row.add_control(self.format_combo)
        quality_min, quality_max = self._output_service.quality_range
        self.quality_row = ParameterRow(ParameterSpec(
            "quality", "compress.quality.label", "Quality", quality_min, quality_max, self._initial_quality,
            "percent"), "", self.quality_group)
        self.quality_slider = self.quality_row.slider
        self.quality_setting_row = self.quality_group.add_row(
            SettingRow(self.quality_group, leading=self.quality_row))
        self.target_row = self.quality_group.add_row(SettingRow(self.quality_group))
        self.target_edit = QLineEdit()
        self.target_edit.setFixedWidth(_NUMBER_WIDTH)
        # 输入完成（回车或失去焦点）时自动匹配
        self.target_edit.editingFinished.connect(self._auto_match_quality)
        self.target_row.add_control(self.target_edit)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        columns.addWidget(self.quality_group, 1, Qt.AlignmentFlag.AlignTop)

        # 尺寸：宽、高与尺寸模式，下面是预计输出
        self.resize_group = SettingSection(self)
        self.width_row = self.resize_group.add_row(SettingRow(self.resize_group))
        self.width_edit = QLineEdit()
        self.width_edit.setFixedWidth(_NUMBER_WIDTH)
        self.width_row.add_control(self.width_edit)
        self.height_row = self.resize_group.add_row(SettingRow(self.resize_group))
        self.height_edit = QLineEdit()
        self.height_edit.setFixedWidth(_NUMBER_WIDTH)
        self.height_row.add_control(self.height_edit)
        self.resize_mode_row = self.resize_group.add_row(SettingRow(self.resize_group))
        self.resize_mode_combo = AppleDropDown()
        self.resize_mode_combo.setFixedWidth(_CHOICE_WIDTH)
        for mode in self._output_service.resize_modes:
            self.resize_mode_combo.addItem(mode, mode)
        self.resize_mode_row.add_control(self.resize_mode_combo)
        columns.addWidget(self.resize_group, 1, Qt.AlignmentFlag.AlignTop)
        root_layout.addLayout(columns)

        # 批量处理列表
        self.batch_group = SettingSection(self)
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.batch_group.add_widget(self.list_widget, 1)
        batch_buttons = QHBoxLayout()
        batch_buttons.setContentsMargins(0, px(SPACING_SM), 0, 0)
        batch_buttons.setSpacing(px(SPACING_SM))
        self.btn_add = AppleCompactButton()
        self.btn_add.clicked.connect(self._add_multi_files)
        self.btn_clear = AppleCompactButton()
        self.btn_clear.clicked.connect(self._clear_list)
        batch_buttons.addWidget(self.btn_add)
        batch_buttons.addWidget(self.btn_clear)
        batch_buttons.addStretch()
        self.batch_group.add_layout(batch_buttons)
        root_layout.addWidget(self.batch_group, 1)

        # 底部：处理中的进度，右下角是取消与主操作
        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(px(SPACING_SM))
        self.progress = UsageBar(self)
        self.progress.setVisible(False)
        footer_layout.addWidget(self.progress, 1)
        footer_layout.addStretch()
        self.btn_cancel = AppleButton()
        self.btn_cancel.clicked.connect(self.reject)
        footer_layout.addWidget(self.btn_cancel)
        self.btn_action = AppleBrandButton()
        self.btn_action.setDefault(True)
        self.btn_action.clicked.connect(self._start_processing)
        footer_layout.addWidget(self.btn_action)
        root_layout.addLayout(footer_layout)

        self.width_edit.textChanged.connect(self._on_resize_changed)
        self.height_edit.textChanged.connect(self._on_resize_changed)
        self.resize_mode_combo.currentIndexChanged.connect(self._on_resize_changed)
        self.quality_row.value_edited.connect(lambda _value: self._invalidate_match())
        self.target_edit.textChanged.connect(self._invalidate_match)

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        self.setWindowTitle(self._tr("compress.window_title", "Image Format Processing"))
        self.source_group.set_title(self._tr("compress.source_group", "Source Selection"))
        self.input_edit.setPlaceholderText(
            self._tr("compress.source.placeholder", "Select image or drag files here...")
        )
        self.btn_browse.setText(self._tr("compress.button.browse", "Browse"))

        self._refresh_source_label()

        self.quality_group.set_title(self._tr("compress.quality_group", "Output Settings"))
        self.quality_row.set_label(plain_label(self._tr("compress.quality.label", "Quality (1-100%):")))
        self.quality_row.setToolTip(self._tr(
            "compress.quality.tooltip", "Quality applies to JPG and WebP; 100% is the highest encoder quality."
        ))
        self.target_row.set_text(plain_label(self._tr("compress.target_size.label", "Target Size (KB):")))
        self.target_edit.setPlaceholderText(
            self._tr("compress.target_size.placeholder", "e.g. 150")
        )
        self._on_format_changed()

        self.resize_group.set_title(self._tr("compress.resize_group", "Resize Options"))
        original_text = self._tr("compress.resize.original", "Original")
        self.width_edit.setPlaceholderText(original_text)
        self.height_edit.setPlaceholderText(original_text)
        for index, mode in enumerate(self._output_service.resize_modes):
            self.resize_mode_combo.setItemText(index, self._tr(f"compress.resize.mode.{mode}"))
        self.width_row.set_text(plain_label(self._tr("compress.resize.width", "Width:")))
        self.height_row.set_text(plain_label(self._tr("compress.resize.height", "Height:")))

        self.batch_group.set_title(self._tr("compress.batch_group", "Batch Processing List"))
        self.btn_add.setText(self._tr("compress.button.add_files", "+ Add Files"))
        self.btn_clear.setText(self._tr("compress.button.clear_list", "Clear List"))
        self.btn_cancel.setText(self._tr("button.cancel", "Cancel"))
        action_key = "compress.button.processing" if self._worker is not None else "compress.button.start"
        self.btn_action.setText(self._tr(action_key))
        self._refresh_previews()

    def _on_format_changed(self, index=None):
        if index is not None:
            self._invalidate_match()
        format_info = self._selected_format()
        self.format_row.set_text(plain_label(self._tr("compress.format.label", "Output Format:")),
                                 self._tr(f"compress.format.hint.{format_info.extension}"))
        # 无损格式没有质量可调：收起质量一行，在格式一行下面说明
        self.quality_setting_row.setVisible(format_info.supports_quality)
        self.format_row.set_message("" if format_info.supports_quality else self._tr("compress.quality.lossless"))
        self.target_row.setEnabled(format_info.supports_target_size)
        self.target_edit.setToolTip("" if format_info.supports_target_size else self._tr(
            "compress.target_size.unsupported_tooltip",
            "Target size matching is available for JPG only.",
        ))
        self._render_status()
        self._refresh_previews()

    def _selected_format(self):
        return self._output_service.output_format(self.format_combo.currentData())

    def _set_format_status(self):
        format_info = self._selected_format()
        if format_info.supports_quality:
            self._set_status("compress.status.ready")
        else:
            key = "compress.status.high_depth" if format_info.bit_depth > 8 else "compress.status.lossless"
            self._set_status(key, format=format_info.label, bit_depth=format_info.bit_depth)

    def _set_status(self, key, kind="info", **values):
        """目标大小一行下面的状态；kind 为 info、success 或 error。"""
        self._status_key, self._status_values, self._status_kind = key, values, kind
        self._render_status()

    def _render_status(self):
        values = dict(self._status_values)
        if "error" in values:
            values["error"] = self._tr(values["error"], values["error"])
        self.target_row.set_message(self._tr(self._status_key, self._status_key, **values), self._status_kind)

    def _resize_options(self):
        return self._output_service.resize_options(
            self.width_edit.text(), self.height_edit.text(), self.resize_mode_combo.currentData())

    def _on_resize_changed(self):
        self._invalidate_match()

    def _invalidate_match(self):
        self._match_generation += 1
        self._pending_match = None
        if self._match_worker is not None:
            self._match_worker.cancellation.cancel()
        self._set_format_status()
        self._refresh_previews()

    def _matching(self):
        return self._pending_match is not None or (
            self._match_worker is not None and self._match_worker.generation == self._match_generation)

    def _refresh_previews(self):
        try:
            options = self._resize_options()
            error = ""
        except ValueError as exc:
            options, error = None, self._tr(str(exc), str(exc))
        self.btn_action.setEnabled(self._worker is None and not self._closing and not self._matching() and not error)
        path = self.input_edit.text().strip()
        info = self._source_info.get(path)
        estimate_kind = "info"
        if error:
            estimate, estimate_kind = error, "error"
        elif info is None:
            estimate = self._tr("compress.resize.estimate_pending", "Estimated output: —")
        else:
            try:
                plan = self._output_service.describe_resize(info.size, options)
                estimate = self._tr("compress.resize.estimate", "Estimated output: {width}×{height}",
                                    width=plan.output_size[0], height=plan.output_size[1])
            except ValueError as exc:
                estimate, estimate_kind = self._tr(str(exc), str(exc)), "error"
                self.btn_action.setEnabled(False)
        self.resize_mode_row.set_message(estimate, estimate_kind)
        mode = self.resize_mode_combo.currentData()
        hint_key = f"compress.resize.hint.{mode}"
        if mode == "pad":
            hint_key += ".transparent" if self._selected_format().supports_alpha else ".white"
        if options is not None:
            if options.width is None and options.height is None:
                hint_key = "compress.resize.hint.original"
            elif options.width is None or options.height is None:
                hint_key = "compress.resize.hint.proportional"
        self.resize_mode_row.set_text(plain_label(self._tr("compress.resize.mode", "Size Mode:")), self._tr(hint_key))
        for index, batch_path in enumerate(self._batch_files):
            item = self.list_widget.item(index)
            batch_info = self._source_info.get(batch_path)
            detail = error or self._source_error_text(batch_path)
            if batch_info is not None and options is not None:
                try:
                    plan = self._output_service.describe_resize(batch_info.size, options)
                    detail = self._tr("compress.batch.dimensions", "{width}×{height} → {out_width}×{out_height}",
                                      width=batch_info.size[0], height=batch_info.size[1],
                                      out_width=plan.output_size[0], out_height=plan.output_size[1])
                except ValueError as exc:
                    detail = self._tr(str(exc), str(exc))
            if not detail:
                detail = self._tr("compress.info.loading", "Reading image…")
            item.setText(os.path.basename(batch_path) + "\n" + detail)
            item.setToolTip(batch_path)

    def _refresh_source_label(self):
        if self._worker is not None:
            return
        path = self.input_edit.text().strip()
        info = self._source_info.get(path)
        if not path:
            self.source_row.set_message(self._tr("compress.info.none", "Image Info: No file selected"))
        elif path in self._source_errors:
            self.source_row.set_message(self._source_error_text(path), "error")
        elif info is None:
            self.source_row.set_message(self._tr("compress.info.loading", "Reading image…"))
        else:
            self.source_row.set_message(self._tr(
                "compress.info.template", "Image Info: {width}x{height} | {fmt} | {size_kb:.1f} KB",
                width=info.size[0], height=info.size[1], fmt=info.format, size_kb=info.file_size / 1024))

    def _source_error_text(self, path):
        error = self._source_errors.get(path)
        if error is None:
            return ""
        if error == "compress.info.not_found":
            return self._tr(error)
        return self._tr("compress.info.read_failed", "Image Info: Failed to read image ({error})",
                        error=self._tr(error, error))

    def _browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            self._tr("compress.dialog.select_image", "Select Image"),
            "",
            SUPPORTED_FORMATS_FILTER,
        )
        if path:
            if path == self.input_edit.text().strip():
                self._on_input_changed(path)
            else:
                self.input_edit.setText(path)

    def _on_input_changed(self, path):
        self._invalidate_match()
        self._source_info.pop(path.strip(), None)
        self._source_errors.pop(path.strip(), None)
        self._schedule_source_info()

    def _schedule_source_info(self):
        self._info_generation += 1
        self._pending_info = None
        if self._info_worker is not None:
            self._info_worker.cancellation.cancel()
        paths = set(self._batch_files) | {self.input_edit.text().strip()}
        self._source_info = {path: info for path, info in self._source_info.items() if path in paths}
        self._source_errors = {path: error for path, error in self._source_errors.items() if path in paths}
        self._info_timer.start()
        self._refresh_source_label()
        self._refresh_previews()

    def _queue_source_info(self):
        if self._closing:
            return
        paths = tuple(dict.fromkeys([self.input_edit.text().strip(), *self._batch_files]))
        paths = tuple(path for path in paths if path and path not in self._source_info and path not in self._source_errors)
        if paths:
            self._pending_info = (self._info_generation, paths)
            self._start_pending_info()

    def _start_pending_info(self):
        if self._info_worker is not None or self._pending_info is None or self._closing:
            return
        generation, paths = self._pending_info
        self._pending_info = None
        self._info_worker = SourceInfoWorker(generation, paths, self._output_service, self)
        self._info_worker.source_ready.connect(self._on_source_ready)
        self._info_worker.finished.connect(self._on_info_finished)
        self._info_worker.start()

    @Slot(int, str, object, str)
    def _on_source_ready(self, generation, path, info, error):
        if self._closing or generation != self._info_generation:
            return
        if error:
            self._source_errors[path] = error
        else:
            self._source_info[path] = info
        self._refresh_source_label()
        self._refresh_previews()

    @Slot()
    def _on_info_finished(self):
        worker = self._info_worker
        self._info_worker = None
        worker.deleteLater()
        self._start_pending_info()
        self._finish_close_if_idle()

    def _auto_match_quality(self):
        if self._closing or self._worker is not None or not self._selected_format().supports_target_size:
            return
        target_text = self.target_edit.text().strip()
        if not target_text:
            return
        path = self.input_edit.text().strip()
        try:
            if not path:
                raise ValueError("compress.status.invalid_source")
            options = self._resize_options()
            target = self._output_service.target_bytes(target_text)
        except ValueError as error:
            self._set_status(str(error), "error")
            return
        self._invalidate_match()
        info = self._source_info.get(path)
        self._pending_match = (self._match_generation, path, options, target, info.stamp if info else None)
        self._set_status("compress.status.matching", dots="…")
        self._start_pending_match()
        self._refresh_previews()

    def _start_pending_match(self):
        if self._match_worker is not None or self._pending_match is None or self._closing:
            return
        request = self._pending_match
        self._pending_match = None
        self._match_worker = QualityMatchWorker(*request, self._output_service, self)
        self._match_worker.finished.connect(self._on_match_finished)
        self._match_worker.start()

    @Slot()
    def _on_match_finished(self):
        worker = self._match_worker
        self._match_worker = None
        worker.deleteLater()
        if not self._closing and worker.generation == self._match_generation:
            if worker.error is not None:
                self._set_status("compress.status.match_failed", "error", error=str(worker.error))
            elif worker.result.quality is None:
                self._set_status("compress.status.target_unreachable", "error")
            else:
                # 程序回填，不算用户改动，匹配结果因此保留
                self.quality_row.set_value(worker.result.quality)
                self._set_status("compress.status.match_result", "success", size_kb=worker.result.file_size / 1024)
        self._start_pending_match()
        self._refresh_previews()
        self._finish_close_if_idle()

    def _add_multi_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            self._tr("compress.dialog.select_images", "Select Images"),
            "",
            SUPPORTED_FORMATS_FILTER,
        )
        if not files:
            return

        existing = set(self._batch_files)
        for path in files:
            if path in existing:
                continue
            existing.add(path)
            self._batch_files.append(path)
            self.list_widget.addItem(QListWidgetItem(path))
        self._schedule_source_info()

    def _clear_list(self):
        self._batch_files.clear()
        self.list_widget.clear()
        self._schedule_source_info()

    def _start_processing(self):
        if self._closing or self._worker is not None or self._matching():
            return
        paths = tuple(self._batch_files)
        if not paths:
            path = self.input_edit.text().strip()
            if not path:
                self.show_warning(self._tr("compress.warning.title", "Wait"),
                                  self._tr("compress.warning.add_one", "Please add at least one image."))
                return
            paths = (path,)
        try:
            options = self._resize_options()
        except ValueError as error:
            self.show_warning(self._tr("compress.warning.title", "Wait"), self._tr(str(error), str(error)))
            return
        self._invalidate_match()

        output_dir = QFileDialog.getExistingDirectory(
            self,
            self._tr("compress.dialog.select_output", "Select Output Folder"),
        )
        if not output_dir:
            return

        qual = self.quality_row.value()
        extension = self.format_combo.currentData()

        tasks = []
        for f in paths:
            name_no_ext = os.path.splitext(os.path.basename(f))[0]
            out_p = os.path.join(output_dir, f"{name_no_ext}_processed.{extension}")
            info = self._source_info.get(f)
            tasks.append((f, out_p, qual, options, info.stamp if info else None))

        self.btn_action.setEnabled(False)
        self.btn_action.setText(self._tr("compress.button.processing", "PROCESSING..."))
        self._task_count = len(tasks)
        self.progress.set_progress(0, self._task_count)
        self.progress.set_text("")
        self.progress.setVisible(True)

        self._export_generation += 1
        self._worker = ImageProcessingWorker(self._export_generation, tasks, self._output_service, self)
        self._worker.progress_updated.connect(self._on_worker_progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._set_status("compress.button.processing")
        self._set_controls_enabled(False)
        self._worker.start()

    @Slot(int, int, str)
    def _on_worker_progress(self, generation, index, name):
        if self._closing or self._worker is None or generation != self._export_generation:
            return
        self.progress.set_progress(index + 1, self._task_count)
        self.progress.set_text(self._tr("compress.status.handling", "Handling: {name}...", name=name))

    @Slot()
    def _on_worker_finished(self):
        worker = self._worker
        result = worker.result
        self._worker = None
        worker.deleteLater()
        self.progress.setVisible(False)
        if self._closing:
            self._finish_close_if_idle()
            return
        self._set_controls_enabled(True)
        self.btn_action.setText(self._tr("compress.button.start", "START PROCESSING"))
        self.source_row.set_message(self._tr(
            "compress.status.task_done", "Task Done! Success: {success}, Failed: {failed}",
            success=len(result.succeeded), failed=len(result.failed)),
            "success" if not result.failed else "warning")
        self._on_format_changed()
        self._invalidate_match()
        self.show_info(self._tr("compress.dialog.finished_title", "Finished"), batch_message(result))

    def _set_controls_enabled(self, enabled):
        for group in (self.source_group, self.quality_group, self.resize_group, self.batch_group):
            group.setEnabled(enabled)

    def _request_close(self):
        self._closing = True
        self._info_timer.stop()
        self._pending_info = None
        self._invalidate_match()
        for worker in (self._worker, self._info_worker, self._match_worker):
            if worker is not None:
                worker.cancellation.cancel()
        self._set_controls_enabled(False)
        self.btn_action.setEnabled(False)
        self._set_status("export.cancelling")

    def _finish_close_if_idle(self):
        if self._closing and all(worker is None for worker in (self._worker, self._info_worker, self._match_worker)):
            super().reject()

    def reject(self):
        self._request_close()
        self._finish_close_if_idle()

    def closeEvent(self, event):
        self._request_close()
        if any(worker is not None for worker in (self._worker, self._info_worker, self._match_worker)):
            event.ignore()
            return
        super().closeEvent(event)
