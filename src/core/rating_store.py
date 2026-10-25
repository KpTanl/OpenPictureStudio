"""图片评分存储 —— QSettings 持久化，按图片路径 hash 索引。

设计要点：
  - 评分 0-5，0 表示未评分（等价于"清除"）
  - 持久化到 QSettings（Windows 注册表 / Linux .conf / macOS plist）
  - 路径 → SHA1 hash 作为 QSettings 键，避免路径分隔符与编码问题
  - 内存缓存避免重复读盘
  - rating_changed 信号驱动 UI 实时刷新（缩略图星标、状态栏等）
  - 全局单例 get_rating_store() 提供访问入口
"""

from __future__ import annotations

import os
import hashlib
import logging

from PyQt6.QtCore import QObject, QSettings, pyqtSignal

from ..utils.app_info import APP_NAME

logger = logging.getLogger(__name__)


def matches_rating(rating: int, mode: str) -> bool:
    if not mode or mode == "all":
        return True
    if mode == "rated":
        return rating > 0
    if mode == "unrated":
        return rating == 0
    if mode.endswith("+"):
        try:
            return rating >= int(mode[:-1])
        except ValueError:
            return True
    if mode.isdigit():
        return rating == int(mode)
    return True


def _path_key(path: str) -> str:
    """将路径转为 QSettings 安全的键名（SHA1 哈希）。

    normcase：Windows 上统一大小写 + 斜杠方向，避免 ``C:\\A.jpg`` 与 ``c:/a.jpg``
    被当成两张不同的图导致评分"丢失"；在 POSIX 上 normcase 是恒等，不影响大小写敏感。
    """
    norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


class RatingStore(QObject):
    """评分存储：0-5，0 表示未评分。

    单实例使用 —— 通过 ``get_rating_store()`` 获取。
    """

    rating_changed = pyqtSignal(str, int)  # (path, new_value)
    save_failed = pyqtSignal(str)

    SETTINGS_GROUP = APP_NAME
    SETTINGS_KEY = "Ratings"

    def __init__(self):
        super().__init__()
        self._settings = QSettings(self.SETTINGS_GROUP, self.SETTINGS_KEY)
        # 内存缓存：path → rating（避免每次读盘）
        self._cache: dict[str, int] = {}
        self._marks = QSettings(self.SETTINGS_GROUP, "PhotoMarks")
        self._mark_cache = {}
        self._library_read = lambda path: None
        self._library_write = None
        self._library_unavailable = lambda: ""
        self._library_has_ratings = lambda: False
        self._library_defer = lambda operation: False
        # 图库首次加载期间排队的写入：(字段, 规范路径) → 排队项；排队期间读取返回排队的值
        self._queued = {}
        self._sequence = 0

    def set_library(self, read, write, *, unavailable=lambda: "", has_ratings=lambda: False, defer=lambda operation: False):
        """read(path) 返回已登记照片的 (评分, 标记) 或 None；write(paths, done=None, **values) 写入已登记照片，
        写入可见后回调 done(value, error)；unavailable() 返回图库资料不可用的原因键；
        defer(operation) 在图库首次加载期间把 operation 排到加载结束之后执行并返回 True，其余时候返回 False。"""
        self._library_read, self._library_write = read, write
        self._library_unavailable, self._library_has_ratings = unavailable, has_ratings
        self._library_defer = defer

    def _defer(self, paths, field, value, write):
        """图库首次加载期间，写入按提交顺序排到加载结束之后；返回是否已排队。

        加载结束时才知道哪些照片已登记，届时 write(done) 按常规路由写入，写入可见后撤下排队值；
        失败时同样撤下、恢复显示已保存的值，原因经 save_failed 报告（图库写入失败由图库运行器报告）。"""
        self._sequence += 1
        entry = (self._sequence, value)
        keys = [(field, self._cache_key(path)) for path in paths]

        def settle(*_result):
            for key in keys:
                if self._queued.get(key) is entry:
                    del self._queued[key]
            self._notify(paths, field)

        def replay():
            try:
                write(settle)
            except ValueError as error:
                settle()
                self.save_failed.emit(str(error))

        if not self._library_defer(replay):
            return False
        self._queued.update(dict.fromkeys(keys, entry))
        self._notify(paths, field)
        return True

    def _queued_value(self, field, path):
        entry = self._queued.get((field, self._cache_key(path))) if self._queued else None
        return entry[1] if entry is not None else None

    def pending_paths(self):
        return frozenset(path for _field, path in self._queued)

    def _notify(self, paths, field):
        if field == "mark":
            self.marks_changed.emit()
            return
        for path in paths:
            self.rating_changed.emit(path, self.get_rating(path))

    def legacy_values(self, path):
        raw = self._settings.value(_path_key(path), 0)
        try:
            rating = max(0, min(5, int(raw)))
        except (TypeError, ValueError):
            rating = 0
        mark = self._marks.value(_path_key(path), "unmarked")
        return rating, mark if mark in ("keep", "reject") else "unmarked"

    def _route_library(self, paths, done=None, **values):
        """已登记照片写入图库（写入可见后回调 done），返回未登记照片；没有已登记照片时由调用方收尾。"""
        if self._library_write is None:
            return tuple(paths)
        reason = self._library_unavailable()
        if reason:
            raise ValueError(reason)
        registered, legacy = [], []
        for path in paths:
            (registered if self._library_read(path) is not None else legacy).append(path)
        if registered:
            self._library_write(tuple(registered), done=done, **values)
        return tuple(legacy)

    marks_changed = pyqtSignal()

    @staticmethod
    def _cache_key(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def get_mark(self, path):
        queued = self._queued_value("mark", path)
        if queued is not None:
            return queued
        photo = self._library_read(path)
        if photo is not None:
            return photo[1]
        key = self._cache_key(path)
        if key not in self._mark_cache:
            value = self._marks.value(_path_key(path), "unmarked")
            self._mark_cache[key] = value if value in ("keep", "reject") else "unmarked"
        return self._mark_cache[key]

    def set_marks(self, paths, value):
        if value not in ("keep", "reject", "unmarked"):
            raise ValueError("browser.invalid_mark")
        paths = tuple(dict.fromkeys(paths))
        if not self._defer(paths, "mark", value, lambda done: self._write_marks(paths, value, done)):
            self._write_marks(paths, value)

    def _write_marks(self, paths, value, done=None):
        legacy = self._route_library(paths, done, mark=value)
        for path in legacy:
            if value == "unmarked":
                self._marks.remove(_path_key(path))
            else:
                self._marks.setValue(_path_key(path), value)
        self._marks.sync()
        self._mark_cache.clear()
        self.marks_changed.emit()
        if done is not None and len(legacy) == len(paths):
            done()
        if self._marks.status() != QSettings.Status.NoError:
            raise ValueError("browser.save_failed")

    def set_ratings(self, paths, value):
        if type(value) is not int or not 0 <= value <= 5:
            raise ValueError("browser.invalid_rating")
        paths = tuple(dict.fromkeys(paths))
        if not self._defer(paths, "rating", value, lambda done: self._write_ratings(paths, value, done)):
            self._write_ratings(paths, value)

    def _write_ratings(self, paths, value, done=None):
        legacy = self._route_library(paths, done, rating=value)
        # 与现有值相同则跳过，减少落盘和 UI 抖动
        changed = [path for path in legacy if self._legacy_rating(path) != value]
        for path in changed:
            if value:
                self._settings.setValue(_path_key(path), value)
            else:
                self._settings.remove(_path_key(path))
            self._cache[self._cache_key(path)] = value
        if changed:
            self._settings.sync()
        for path in changed:
            self.rating_changed.emit(path, self.get_rating(path))
        if done is not None and len(legacy) == len(paths):
            done()
        if self._settings.status() != QSettings.Status.NoError:
            raise ValueError("browser.save_failed")

    def get_rating(self, path: str) -> int:
        """返回评分 0-5。未评分返回 0。"""
        if not path:
            return 0
        queued = self._queued_value("rating", path)
        if queued is not None:
            return queued
        photo = self._library_read(path)
        if photo is not None:
            return photo[0]
        return self._legacy_rating(path)

    def _legacy_rating(self, path):
        cache_key = self._cache_key(path)
        if cache_key in self._cache:
            return self._cache[cache_key]
        key = _path_key(path)
        raw = self._settings.value(key, 0)
        try:
            value = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(5, value))

        self._cache[cache_key] = value
        return value

    def set_rating(self, path: str, value: int) -> None:
        """设置评分 0-5。0 表示清除该评分。

        自动落盘（QSettings.setValue + sync）—— 按键即保存，
        符合 Lightroom 风格"零延迟无确认"的 culling 工作流。
        """
        if not path:
            return
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        value = max(0, min(5, value))
        try:
            self.set_ratings((path,), value)
        except ValueError as error:
            self.save_failed.emit(str(error))

    def clear_all(self) -> int:
        """清除所有评分。返回被清除的条目数（= 持久化里的评分条目数，精确值）。"""
        # 持久化里每个评分占一个键，allKeys() 的长度就是真实评分总数。
        # 缓存里混有浏览缩略图时写入的 0（未评分），不能用来计数。
        if self._library_write is not None:
            try:
                self._library_write(None, rating=0)
            except ValueError as error:
                self.save_failed.emit(str(error))
                return 0
        count = len(self._settings.allKeys())

        # 需要立即刷新 UI 的 path：缓存里评分 > 0 的那些（屏幕上有星标的）
        refresh_paths = [p for p, v in self._cache.items() if v > 0]

        self._settings.clear()
        self._settings.sync()
        self._cache.clear()

        for path in refresh_paths:
            self.rating_changed.emit(path, 0)
        return count

    def has_any(self) -> bool:
        """是否存在任何评分（用于「清除」按钮的禁用判定）。

        缓存里会混入浏览缩略图时写入的 0（未评分），不能因为缓存非空就提前返回；
        必须同时检查持久化里是否还有未加载到内存的评分条目。
        """
        if (self._library_has_ratings() or any(v > 0 for v in self._cache.values())
                or any(field == "rating" and entry[1] > 0 for (field, _key), entry in self._queued.items())):
            return True
        return bool(self._settings.allKeys())


# ---------------------------------------------------------------- Singleton
_rating_store: RatingStore | None = None


def get_rating_store() -> RatingStore:
    """获取全局评分存储实例。"""
    global _rating_store
    if _rating_store is None:
        _rating_store = RatingStore()
    return _rating_store
