"""选片缩略图：编辑结果就是源画面的照片沿用源缩略图，其余照片取编辑结果。"""

from __future__ import annotations

from types import SimpleNamespace

from src.application.photo_browser import PhotoBrowserService
from tests.test_photo_batch_library_fixes import _color, _photo, _workspace


def test_only_unedited_non_raw_photos_show_their_source(tmp_path):
    core, drafts, sessions = _workspace(tmp_path)
    plain, edited = _photo(tmp_path / "plain.jpg"), _photo(tmp_path / "edited.jpg")
    raw = str(tmp_path / "photo.nef")
    drafts.update_drafts(edited, {"color_adjust": _color(exposure_amt=100)})
    service = PhotoBrowserService(SimpleNamespace(), SimpleNamespace(), is_raw=lambda path: path.endswith(".nef"),
                                  drafts_ready=lambda: True)
    assert not service.shows_source(plain)  # 还没接上编辑会话时不知道有没有编辑，按编辑结果处理
    service.sessions = sessions
    assert service.shows_source(plain)
    assert not service.shows_source(edited)
    assert not service.shows_source(raw)
    service.image_mode = "ops"  # 非 RAW 选了 OPS 观感，结果不再是源画面
    assert not service.shows_source(plain)
