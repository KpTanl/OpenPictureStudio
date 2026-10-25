import os

from .image_listing import directory_images, existing_images


class ImageManager:
    def __init__(self):
        self._current_path: str | None = None
        self._current_revision = 0
        self._list_version = 0
        self.image_list: list[str] = []
        self.current_index: int = -1
        self._rating_filter: str = "all"
        self._filtered_provider = None

    def set_filtered_provider(self, provider):
        self._filtered_provider = provider

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @current_path.setter
    def current_path(self, path: str | None) -> None:
        self._current_path = path
        self._current_revision += 1

    @property
    def current_revision(self) -> int:
        return self._current_revision

    @property
    def list_version(self) -> int:
        return self._list_version

    def publish_images(self, paths, *, append=False, preferred_path=None, expected_version=None) -> bool:
        if expected_version is not None and expected_version != self._list_version:
            return False
        paths = list(dict.fromkeys(paths))
        if not paths:
            return False
        if append:
            existing = set(self.image_list)
            paths = self.image_list + [path for path in paths if path not in existing]
        self.image_list = paths
        self._list_version += 1
        if preferred_path in paths and preferred_path != self.current_path:
            self.current_path = preferred_path
        elif self.current_path not in paths:
            self.current_path = paths[0]
        self.current_index = paths.index(self.current_path)
        return True

    def reorder_images(self, paths, *, expected_version) -> bool:
        if (expected_version != self._list_version or len(paths) != len(self.image_list)
                or len(set(paths)) != len(paths) or set(paths) != set(self.image_list)):
            return False
        return self.publish_images(paths)

    def open_file(self, path: str, load_siblings: bool = False, clear_list: bool = True) -> bool:
        path = os.path.normpath(path)
        if not os.path.isfile(path):
            return False

        paths = directory_images(os.path.dirname(path) or ".") if clear_list and load_siblings else [path]
        if path not in paths:
            paths.append(path)
            paths.sort()
        return self.publish_images(paths, append=not clear_list, preferred_path=path)

    def select_image(self, path: str) -> bool:
        if path not in self.image_list:
            return False
        self.current_path = path
        try:
            self.current_index = self.image_list.index(path)
        except ValueError:
            self.current_index = 0
        return True

    def replace_current_image(self, new_path: str) -> bool:
        new_path = os.path.normpath(new_path)
        if not os.path.isfile(new_path):
            return False
        if 0 <= self.current_index < len(self.image_list):
            self.image_list[self.current_index] = new_path
        else:
            self.image_list = [new_path]
            self.current_index = 0
        self._list_version += 1
        self.current_path = new_path
        return True

    def open_files(self, paths: list[str], append: bool = True) -> bool:
        valid_paths = existing_images(paths)
        if not valid_paths:
            return False

        keep_current = self.current_path in valid_paths or append and self.current_path in self.image_list
        preferred = self.current_path if keep_current else valid_paths[0]
        return self.publish_images(valid_paths if append else sorted(valid_paths),
                                   append=append, preferred_path=preferred)

    def open_folder(self, folder_path: str, append: bool = True) -> bool:
        if not os.path.isdir(folder_path):
            return False

        folder_images = directory_images(folder_path)

        if not folder_images:
            return False

        return self.publish_images(folder_images, append=append)

    def set_rating_filter(self, filter_mode: str) -> None:
        self._rating_filter = filter_mode

    def get_rating_filter(self) -> str:
        return self._rating_filter

    def matches_filter(self, path: str) -> bool:
        if self._filtered_provider is not None:
            return path in self._filtered_provider()
        if not self._rating_filter or self._rating_filter == "all":
            return True
        from .rating_store import get_rating_store, matches_rating
        return matches_rating(get_rating_store().get_rating(path), self._rating_filter)

    def get_filtered_images(self) -> list[str]:
        if self._filtered_provider is not None:
            return list(self._filtered_provider())
        if not self._rating_filter or self._rating_filter == "all":
            return list(self.image_list)
        return [p for p in self.image_list if self.matches_filter(p)]

    def next_image(self) -> str | None:
        filtered = self.get_filtered_images()
        if not filtered:
            return None

        if not self.current_path or self.current_path not in filtered:
            self.current_path = filtered[0]
            try:
                self.current_index = self.image_list.index(self.current_path)
            except ValueError:
                self.current_index = 0
            return self.current_path

        idx = filtered.index(self.current_path)
        if idx + 1 < len(filtered):
            self.current_path = filtered[idx + 1]
            try:
                self.current_index = self.image_list.index(self.current_path)
            except ValueError:
                self.current_index = 0
            return self.current_path
        return None

    def prev_image(self) -> str | None:
        filtered = self.get_filtered_images()
        if not filtered:
            return None

        if not self.current_path or self.current_path not in filtered:
            self.current_path = filtered[0]
            try:
                self.current_index = self.image_list.index(self.current_path)
            except ValueError:
                self.current_index = 0
            return self.current_path

        idx = filtered.index(self.current_path)
        if idx > 0:
            self.current_path = filtered[idx - 1]
            try:
                self.current_index = self.image_list.index(self.current_path)
            except ValueError:
                self.current_index = 0
            return self.current_path
        return None

    def has_next(self) -> bool:
        filtered = self.get_filtered_images()
        if not filtered or not self.current_path:
            return False
        if self.current_path not in filtered:
            return len(filtered) > 0
        return filtered.index(self.current_path) + 1 < len(filtered)

    def has_prev(self) -> bool:
        filtered = self.get_filtered_images()
        if not filtered or not self.current_path:
            return False
        if self.current_path not in filtered:
            return False
        return filtered.index(self.current_path) > 0

    def get_image_info(self) -> dict:
        if not self.current_path or not os.path.isfile(self.current_path):
            return {}
        stat = os.stat(self.current_path)
        return {
            "path": self.current_path,
            "name": os.path.basename(self.current_path),
            "size_kb": round(stat.st_size / 1024, 1),
        }
