import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtGui import QColor, QFocusEvent, QImage
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import Shiboken

from src.ui.dialogs.photo_batch_dialog import PhotoBatchDialog
from src.ui.dialogs.settings_dialog import SettingsDialog
from src.ui.dialogs.tool_manager_page import ToolManagerPage
from src.ui.panels.id_photo_panel import _rgb_image
from src.ui.services.background_operation import run_background, start_background
from src.ui.services.display_histogram_runner import _sample_frame
from src.ui.widgets.assistant_context_inspector import ContextInspector
from src.ui.widgets.assistant_transcript import AssistantTranscript
from src.ui.widgets.canvas_toolbar import _ZoomReadout
from src.ui.widgets.thumbnail_panel import ThumbnailPanel
from src.ui.widgets.value_labels import EditableValueLabel
from tests.support import wait_for_studio, wait_for_thumbnails, wait_until


def test_container_signals_keep_the_original_object(qapp, image_sources, panel_factory, tmp_path, window_factory):
    path = tmp_path / "signal.png"
    Image.new("RGB", (20, 20), "red").save(path)
    window = window_factory()
    window.show()
    window.manager.open_file(str(path))
    window._refresh_all()
    studio = wait_for_studio(window, str(path))
    widgets = [
        (SettingsDialog(), "tools_changed", ("first", "second")),
        (ToolManagerPage(studio.core), "tools_changed", ("first", "second")),
        (panel_factory("color_adjust", str(path)), "params_changed", {1: object()}),
        (ContextInspector(), "edited", {2: object()}),
        (ThumbnailPanel(image_source_service=image_sources), "order_changed", ["first", "second"]),
    ]
    for widget, name, payload in widgets:
        received = []
        signal = getattr(widget, name)
        signal.connect(received.append)
        signal.emit(payload)
        assert len(received) == 1 and received[0] is payload
    widgets[-1][0].shutdown()
    for widget, _, _ in widgets:
        QWidget.close(widget)


def test_both_thumbnail_panels_reorder_the_main_window(window_factory, tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (20, 20), (index * 50, 10, 20)).save(path)
        paths.append(str(path))
    window = window_factory()
    window.show()
    window.manager.open_files(paths, append=False)
    window._refresh_all()
    wait_for_thumbnails(window.thumbnail_panel, paths, paths[0], version=window.manager.list_version)
    viewer_order = [paths[2], paths[0], paths[1]]
    window.thumbnail_panel.order_changed.emit(viewer_order)
    assert window.manager.image_list == viewer_order
    studio = wait_for_studio(window, paths[0])
    panel = studio.thumbnail_panel
    wait_for_thumbnails(panel, viewer_order, paths[0], version=window.manager.list_version)
    studio_order = [paths[1], paths[2], paths[0]]
    panel.order_changed.emit(studio_order)
    assert window.manager.image_list == studio_order
    assert window.manager.current_path == paths[0]


def test_background_results_are_delivered_on_the_gui_thread(qapp, monkeypatch):
    gui = threading.get_ident()
    workers, callbacks, dialogs = [], [], []
    from src.ui.widgets.progress_dialog import ProgressDialog
    finish = ProgressDialog.finish

    def finish_on_thread(dialog):
        dialogs.append(threading.get_ident())
        return finish(dialog)

    monkeypatch.setattr(ProgressDialog, "finish", finish_on_thread)

    def operation(_cancel):
        workers.append(threading.get_ident())
        return 42

    assert run_background(None, operation) == 42
    start_background(operation, lambda value, error: callbacks.append((threading.get_ident(), value, error)))
    wait_until(lambda: bool(callbacks), "后台结果送达")
    assert len(workers) == 2 and all(worker != gui for worker in workers)
    assert callbacks == [(gui, 42, None)]
    assert dialogs == [gui]


def test_histogram_frame_is_an_independent_rgba_copy(qapp):
    image = QImage(2, 1, QImage.Format.Format_RGBA8888)
    image.setPixelColor(0, 0, QColor(10, 20, 30, 40))
    image.setPixelColor(1, 0, QColor(90, 100, 110, 120))
    size, data = _sample_frame(image)
    image.fill(QColor(1, 2, 3, 4))
    assert size == (2, 1)
    assert data == bytes((10, 20, 30, 40, 90, 100, 110, 120))


def test_rgb888_padded_rows_are_copied_without_shifting(qapp):
    image = QImage(301, 3, QImage.Format.Format_RGB888)
    for y, color in enumerate((QColor(200, 10, 20), QColor(30, 180, 40), QColor(50, 60, 170))):
        for x in range(image.width()):
            image.setPixelColor(x, y, color)
    assert image.bytesPerLine() > image.width() * 3
    result = _rgb_image(image)
    image.fill(QColor(0, 0, 0))
    assert [result.getpixel((300, y)) for y in range(3)] == [(200, 10, 20), (30, 180, 40), (50, 60, 170)]


def test_queued_transcript_updates_stop_when_widget_is_destroyed(qapp):
    transcript = AssistantTranscript()
    transcript.set_messages("conversation", [], lambda key: key)
    transcript._user_scroll(0)
    Shiboken.delete(transcript)
    QApplication.processEvents()


def test_destroyed_batch_dialog_is_disconnected_from_runner(qapp):
    class Runner(QObject):
        changed = Signal(str)

    runner = Runner()
    runner.service = SimpleNamespace(batches=lambda: ())
    dialog = PhotoBatchDialog(runner, lambda: None, lambda path: None)
    Shiboken.delete(dialog)
    runner.changed.emit("batch")
    QApplication.processEvents()


@pytest.mark.parametrize("factory", [lambda parent: _ZoomReadout(parent), lambda parent: EditableValueLabel("42", parent)],
                         ids=["zoom", "value"])
def test_delayed_selection_stops_when_editor_is_destroyed(qapp, factory):
    host = QWidget()
    editor = factory(host)
    editor.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    Shiboken.delete(host)
    QApplication.processEvents()
