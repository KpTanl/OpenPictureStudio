from threading import Event

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog

from src.ui.services.transform_save import TransformSaveWorker
from src.ui.widgets.message_dialog import MessageDialog
from tests.support import wait_for_studio, wait_for_thumbnails, wait_for_viewer


def wait_for_canvas(qtbot, win, image):
    rgba = image.convert("RGBA")
    expected = QImage(
        rgba.tobytes(),
        rgba.width,
        rgba.height,
        rgba.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()
    qtbot.waitUntil(
        lambda: win.viewer._pixmap is not None
        and win.viewer._pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        == expected
    )


@pytest.fixture
def window(window_factory, tmp_path):
    win = window_factory()
    win._persist_geometry = False
    win.show()
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    for path, color in zip(paths, ("red", "blue")):
        image = Image.new("RGB", (40, 30), color)
        image.paste("green", (0, 0, 15, 10))
        image.save(path)
    win.manager.open_files([str(path) for path in paths], append=False)
    win._refresh_all()
    wait_for_viewer(win, paths[0])
    wait_for_thumbnails(win.thumbnail_panel, paths, paths[0],
                        version=win.manager.list_version)
    return win, paths


@pytest.fixture
def blocked_save(monkeypatch):
    entered, release = Event(), Event()
    requests = []
    original = TransformSaveWorker.run

    def run(worker):
        if not requests:
            requests.append(worker._request)
            entered.set()
            if not release.wait(30):
                worker.signals.finished.emit(worker._request, "测试等待超时")
                return
        original(worker)

    monkeypatch.setattr(TransformSaveWorker, "run", run)
    yield entered, release, requests
    release.set()
    assert QThreadPool.globalInstance().waitForDone(10000)


@pytest.mark.parametrize("navigation", ["next", "thumbnail", "roundtrip"])
def test_late_transform_cannot_replace_selected_image(
    window, blocked_save, qtbot, navigation
):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    if navigation == "next":
        win._next_image()
    else:
        win._on_thumbnail_selected(str(paths[1]))
    if navigation == "roundtrip":
        win._prev_image()
    expected = str(paths[0] if navigation == "roundtrip" else paths[1])
    release.set()
    qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
    assert win.manager.current_path == expected
    assert win.manager.image_list == [str(path) for path in paths]
    assert win._current_pil_image is None
    with Image.open(expected) as image:
        wait_for_canvas(qtbot, win, image)
    for panel in win._thumbnail_panels():
        assert panel.get_selected_paths() == [expected]


def test_new_transform_wins_over_old_save_and_uses_new_photo(
    window, blocked_save, qtbot
):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    win._on_thumbnail_selected(str(paths[1]))
    wait_for_viewer(win, paths[1])
    win._flip_horizontal()
    qtbot.waitUntil(
        lambda: win.manager.current_path not in [str(path) for path in paths]
    )
    saved = win.manager.current_path
    release.set()
    qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
    assert win.manager.current_path == saved
    assert win.manager.current_index == 1
    assert win.manager.image_list == [str(paths[0]), saved]
    with Image.open(paths[1]) as source, Image.open(saved) as output:
        expected = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        np.testing.assert_array_equal(np.asarray(output), np.asarray(expected))
        wait_for_canvas(qtbot, win, expected)
    for panel in win._thumbnail_panels():
        assert panel.get_selected_paths() == [saved]


def test_continuous_transforms_keep_only_latest_result(window, blocked_save, qtbot):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    win._flip_vertical()
    qtbot.waitUntil(lambda: win.manager.current_path != str(paths[0]))
    saved = win.manager.current_path
    release.set()
    qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
    assert win.manager.current_path == saved
    with Image.open(paths[0]) as source, Image.open(saved) as output:
        expected = source.rotate(-90, expand=True).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM
        )
        np.testing.assert_array_equal(np.asarray(output), np.asarray(expected))
        wait_for_canvas(qtbot, win, expected)
    win._next_image()
    win._prev_image()
    assert win.manager.current_path == saved


def test_close_invalidates_pending_save(window, blocked_save, qtbot):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    win.close()
    qtbot.waitUntil(lambda: win._caches_closed_for_exit)
    release.set()
    assert QThreadPool.globalInstance().waitForDone(10000)
    qtbot.wait(1)
    assert win.manager.current_path == str(paths[0])
    assert not requests[0].target_path.parent.exists()


def test_failed_save_keeps_source_and_reports_error(window, qtbot, monkeypatch):
    win, paths = window

    def fail(image, *args, **kwargs):
        raise OSError("模拟编码失败")

    monkeypatch.setattr(Image.Image, "save", fail)
    win._rotate_90()
    qtbot.waitUntil(lambda: "模拟编码失败" in win.toast.text() and not win.toast.isHidden())
    assert win.manager.current_path == str(paths[0])
    assert win.manager.image_list == [str(path) for path in paths]
    assert win._current_pil_image is not None
    assert not win._temp_paths


def test_image_revision_rejects_roundtrip_without_ui_generation_change(
    window, blocked_save, qtbot
):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    generation = win._save_generation
    win.manager.select_image(str(paths[1]))
    win.manager.select_image(str(paths[0]))
    assert win._save_generation == generation
    release.set()
    qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
    assert win.manager.current_path == str(paths[0])
    assert win.manager.image_list == [str(path) for path in paths]


def test_save_as_keeps_snapshot_when_background_save_finishes_in_dialog(
    window,
    blocked_save,
    qtbot,
    monkeypatch,
    tmp_path,
):
    win, paths = window
    entered, release, _ = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    output = tmp_path / "saved.png"

    def choose_path(*args, **kwargs):
        release.set()
        qtbot.waitUntil(lambda: win.manager.current_path != str(paths[0]))
        return str(output), "PNG"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_path)
    monkeypatch.setattr(MessageDialog, "run", lambda self: "ok")
    win._save_as()
    with Image.open(paths[0]) as source, Image.open(output) as image:
        np.testing.assert_array_equal(
            np.asarray(image), np.asarray(source.rotate(-90, expand=True))
        )


@pytest.mark.parametrize("change", ["image", "transform"])
def test_save_as_keeps_snapshot_when_dialog_changes_view(
    window, blocked_save, qtbot, monkeypatch, tmp_path, change
):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    output = tmp_path / "snapshot.png"
    selected = []

    def choose_path(*args, **kwargs):
        if change == "image":
            win._next_image()
            wait_for_viewer(win, paths[1])
        else:
            win._flip_vertical()
            qtbot.waitUntil(lambda: win.manager.current_path != str(paths[0]))
        selected.append(win.manager.current_path)
        release.set()
        qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
        return str(output), "PNG"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_path)
    monkeypatch.setattr(MessageDialog, "run", lambda self: "ok")
    win._save_as()
    assert win.manager.current_path == selected[0]
    with Image.open(paths[0]) as source, Image.open(output) as image:
        np.testing.assert_array_equal(
            np.asarray(image), np.asarray(source.rotate(-90, expand=True))
        )


def test_cancel_save_as_preserves_pending_transform(window, blocked_save, qtbot, monkeypatch):
    win, paths = window
    entered, release, _ = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    image = win._current_pil_image
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    win._save_as()
    assert win.manager.current_path == str(paths[0])
    assert win._current_pil_image is image
    release.set()
    qtbot.waitUntil(lambda: win.manager.current_path != str(paths[0]))
    with Image.open(paths[0]) as source, Image.open(win.manager.current_path) as output:
        np.testing.assert_array_equal(
            np.asarray(output), np.asarray(source.rotate(-90, expand=True))
        )


def test_save_as_multiple_images_reads_each_source(
    window, blocked_save, qtbot, monkeypatch, tmp_path
):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    win.thumbnail_panel.list_widget.item(1).setSelected(True)
    assert win.thumbnail_panel.get_selected_paths() == [str(path) for path in paths]
    output = tmp_path / "exports"
    output.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args, **kwargs: str(output))
    monkeypatch.setattr(MessageDialog, "run", lambda self: "ok")
    win._save_as()
    for path in paths:
        with Image.open(path) as source, Image.open(output / path.name) as image:
            np.testing.assert_array_equal(np.asarray(image), np.asarray(source))
    release.set()
    qtbot.waitUntil(lambda: win.manager.current_path == str(requests[0].target_path))


def test_studio_edits_survive_late_viewer_save(window, blocked_save, qtbot):
    win, paths = window
    entered, release, requests = blocked_save
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    studio = wait_for_studio(win, paths[0])
    session = studio.session
    session.color_params["exposure_amt"] = 30
    assert session.sync().success
    before = win.studio_workspace.core.get_session(session.core_id).unwrap()
    release.set()
    qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
    assert win.manager.current_path == str(paths[0])
    assert win.studio_workspace.session is session
    assert win.studio_workspace.core.get_session(session.core_id).unwrap() == before


def test_replacement_invalidates_pending_transform(
    window, blocked_save, qtbot, tmp_path
):
    win, paths = window
    entered, release, requests = blocked_save
    replacement = tmp_path / "replacement.png"
    Image.new("RGB", (22, 18), "yellow").save(replacement)
    win._rotate_90()
    qtbot.waitUntil(entered.is_set)
    assert win.manager.replace_current_image(str(replacement))
    win._update_image_path_in_panels(str(paths[0]), str(replacement))
    win._load_current()
    release.set()
    qtbot.waitUntil(lambda: not requests[0].target_path.parent.exists())
    assert win.manager.current_path == str(replacement)
    with Image.open(replacement) as image:
        wait_for_canvas(qtbot, win, image)


def test_studio_navigation_bounds_pixels_and_preserves_edits(window, qtbot, tmp_path):
    win, paths = window
    for index in range(6):
        path = tmp_path / f"extra-{index}.png"
        Image.new("RGBA", (40, 30), (50, index * 30, 80, 100)).save(path)
        paths.append(path)
    win.manager.open_files([str(path) for path in paths])
    win._refresh_all()
    studio = wait_for_studio(win, paths[0])
    first = studio.session
    first.color_params["exposure_amt"] = 20
    assert first.sync().success
    before = studio.core.get_session(first.core_id).unwrap()
    for path in paths[1:]:
        win._on_thumbnail_selected(str(path))
        wait_for_studio(win, path)
        usage = studio.core.image_sources.resources.usage()
        assert usage["bytes"] + usage["reserved_bytes"] <= studio.core.image_sources.policy.memory_bytes
    assert QThreadPool.globalInstance().waitForDone(10000)
    qtbot.wait(1)
    win._on_thumbnail_selected(str(paths[0]))
    wait_for_studio(win, paths[0])
    assert studio.session is first
    assert studio.core.get_session(first.core_id).unwrap() == before
