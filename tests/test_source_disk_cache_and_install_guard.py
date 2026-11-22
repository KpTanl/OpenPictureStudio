"""磁盘源图缓存编解码校验、安装清单生成及安装防重入守卫。"""

from contextlib import nullcontext
import hashlib
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from src.application.image_resources import ComputeBudget, ImageResourcePolicy, ImageResourcePool
from src.application.managed_cache import ManagedCache
from src.application.source_disk_cache import SourceDiskCodec, SourceDiskKey, validate_index
from src.core.source_input import IntegerSourceInput, SourceInputSpec, materialize_input
from src.utils import installation
from tools.install_manifest import MANIFEST_NAME, build_manifest


class _DiskCacheHost:
    VERSION = "v1"

    def __init__(self, directory):
        self._directory = directory
        self._closed = Event()

    @property
    def capacity_ready(self):
        return self._directory.capacity_ready

    @property
    def is_writer(self):
        return self._directory.is_writer

    def _request_current(self, _request):
        return True


def test_source_disk_key_and_validate_index():
    spec = SourceInputSpec((64, 48), 3, 8)
    digest = "a" * 64
    key = SourceDiskKey(digest, spec, ("png",))
    assert len(key.digest) == 64
    assert SourceDiskKey(digest, spec, ("png",), half_size=True).digest != key.digest
    assert SourceDiskKey(digest, SourceInputSpec((64, 48), 4, 8), ("png",)).digest != key.digest

    for bad_hash in ("short", "A" * 64, "g" * 64):
        with pytest.raises(ValueError):
            SourceDiskKey(bad_hash, spec, ("png",))
    with pytest.raises(ValueError):
        SourceDiskKey(digest, spec, ())

    data = np.zeros(16, dtype=np.uint8)
    valid_blocks = np.array([[0, 6, 0, 10], [8, 5, 10, 8]], dtype="<u4")
    validate_index(data, valid_blocks, 18)

    for bad_blocks, expected in (
        (np.empty((0, 4), dtype="<u4"), 0),
        (np.array([[1, 5, 0, 10], [8, 5, 10, 8]], dtype="<u4"), 18),
        (np.array([[0, 17, 0, 10]], dtype="<u4"), 10),
        (np.array([[0, 6, 0, 9], [8, 5, 10, 8]], dtype="<u4"), 18),
        (valid_blocks, 19),
    ):
        with pytest.raises(ValueError):
            validate_index(data, bad_blocks, expected)


def test_source_disk_codec_roundtrip_and_corruption_cleanup(tmp_path):
    compute = ComputeBudget(2, 4, operator_threads=2)
    resources = ImageResourcePool(ImageResourcePolicy())
    directory = ManagedCache(tmp_path / "src_cache", max_bytes=64 * 1024 * 1024, suffix=".opssrc")
    directory.prepare("v1")
    try:
        host = _DiskCacheHost(directory)
        codec = SourceDiskCodec(host)
        codec.bind(compute, resources)

        # 可压缩渐变图（走 lz4-flex 分块）与不可压缩随机噪声图（走 raw）
        spec = SourceInputSpec((128, 128), 3, 8, container="png")
        gradient = np.broadcast_to(np.arange(128, dtype=np.uint8)[:, None, None], (128, 128, 3)).copy()
        gradient_bytes = np.ascontiguousarray(gradient.reshape(-1))
        gradient_bytes.setflags(write=False)

        noise = np.random.default_rng(42).integers(0, 256, size=spec.nbytes, dtype=np.uint8)
        noise.setflags(write=False)

        for idx, (tag, payload, expect_blocks) in enumerate((
            ("grad", gradient_bytes, True),
            ("noise", noise, False),
        )):
            key = SourceDiskKey(f"{idx:064x}", spec, ("test", tag))
            entry = IntegerSourceInput(spec, payload)
            request = SimpleNamespace(
                key=key,
                lease=SimpleNamespace(value=entry),
                generation=directory.generation,
                preloading=False,
            )
            codec.write(request)
            peeked = codec.peek(key, is_current=lambda: True, is_cancelled=lambda: False)
            assert peeked is not None
            restored = codec.read(key, peeked[0], is_current=lambda: True, is_cancelled=lambda: False)
            assert restored is not None
            assert (restored.blocks is not None) is expect_blocks
            materialized = materialize_input(restored)
            assert np.array_equal(materialized.data, payload)

        # 篡改魔数、元数据、载荷与截断文件均能安全识别并清理坏文件
        target_key = SourceDiskKey(f"{0:064x}", spec, ("test", "grad"))
        target_file = directory._target(target_key.digest, "v1")
        good_bytes = target_file.read_bytes()

        # 1. 载荷最后一字节篡改：peek 头过但 read 载荷 CRC 不匹配并清理
        bad_payload = bytearray(good_bytes)
        bad_payload[-1] ^= 0xFF
        target_file.write_bytes(bad_payload)
        peeked = codec.peek(target_key, is_current=lambda: True, is_cancelled=lambda: False)
        assert peeked is not None
        assert codec.read(target_key, peeked[0], is_current=lambda: True, is_cancelled=lambda: False) is None
        assert not target_file.exists()

        # 2. 元数据篡改 / 魔数篡改 / 截断：peek 直接返回 None 并删除
        for mutate in (
            lambda b: b"BADMAGIC" + b[8:],
            lambda b: b[:32] + bytes([b[32] ^ 0xFF]) + b[33:],
            lambda b: b[:20],
        ):
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(mutate(good_bytes))
            assert codec.peek(target_key, is_current=lambda: True, is_cancelled=lambda: False) is None
            assert not target_file.exists()
    finally:
        directory.close()
        resources.close()
        compute.close()


def test_install_manifest_build_and_rejections(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    exe = bundle / "OpenPictureStudio.exe"
    exe.write_bytes(b"MZ-stub")
    lib = bundle / "_internal" / "core.dll"
    lib.parent.mkdir()
    lib.write_bytes(b"dll-payload")

    sha = "0123456789abcdef" * 2 + "01234567"
    manifest_path = build_manifest(bundle, "0.17.0", sha)
    assert manifest_path == bundle / MANIFEST_NAME
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["OPS-INSTALL\t1", "version\t0.17.0", f"source\t{sha}"]

    entries = [line.split("\t") for line in lines[3:]]
    by_path = {rel: (digest, int(size)) for digest, size, rel in entries}
    assert MANIFEST_NAME not in by_path
    assert by_path["OpenPictureStudio.exe"] == (hashlib.sha256(b"MZ-stub").hexdigest(), 7)
    assert by_path["_internal\\core.dll"] == (hashlib.sha256(b"dll-payload").hexdigest(), 11)

    with pytest.raises(ValueError):
        build_manifest(bundle, "0.17.0-beta", sha)
    with pytest.raises(ValueError):
        build_manifest(bundle, "0.17.0", "deadbeef")

    for forbidden_name in ("unins000.exe", ".install-pending.txt"):
        bad_dir = tmp_path / ("bad_" + forbidden_name.strip("."))
        bad_dir.mkdir()
        (bad_dir / "OpenPictureStudio.exe").write_bytes(b"exe")
        (bad_dir / forbidden_name).write_bytes(b"x")
        with pytest.raises(ValueError):
            build_manifest(bad_dir, "0.17.0", sha)

    missing_exe = tmp_path / "no_exe"
    missing_exe.mkdir()
    with pytest.raises(ValueError):
        build_manifest(missing_exe, "0.17.0", sha)


def test_installation_storage_target_and_startup_guard(tmp_path, monkeypatch):
    app_dir = tmp_path / "Program"
    app_dir.mkdir()

    # 非打包态不拦截
    monkeypatch.setattr(installation, "installation_root", lambda: None)
    installation.check_storage_target(app_dir / "data")

    # 打包态禁止写入安装目录、其子目录及父目录，允许兄弟目录
    monkeypatch.setattr(installation, "installation_root", lambda: app_dir)
    for blocked in (app_dir, app_dir / "data", app_dir.parent):
        with pytest.raises(ValueError, match="storage.error.installation"):
            installation.check_storage_target(blocked)
    sibling = tmp_path / "UserData"
    sibling.mkdir()
    installation.check_storage_target(sibling)

    class _FakeKernel:
        def __init__(self):
            self.closed = []

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return True

    def run_guard(*, maintenance_sequence=(False, False), pending=False, fail=False):
        kernel = _FakeKernel()
        shown = []
        created = []
        handles = []
        states = iter(maintenance_sequence)
        pending_file = app_dir / installation.PENDING_NAME
        pending_file.unlink(missing_ok=True)
        if pending:
            pending_file.write_text("pending", encoding="utf-8")

        def active(_k):
            if fail:
                raise OSError("mutex probe failed")
            return next(states)

        monkeypatch.setattr(installation, "_handles", handles)
        monkeypatch.setattr(installation, "_kernel", lambda: kernel)
        monkeypatch.setattr(installation, "_admission", lambda _k, _name: nullcontext())
        monkeypatch.setattr(installation, "_maintenance_active", active)
        monkeypatch.setattr(installation, "_create_mutex", lambda _k, name: created.append(name) or object())
        monkeypatch.setattr(installation, "_show_startup_error", shown.append)
        result = installation.guard_startup()
        return result, shown, created, kernel.closed, handles

    ok, shown, created, closed, handles = run_guard(maintenance_sequence=(False, False))
    assert ok is True and shown == [] and created == [installation.APPLICATION_MUTEX] and closed == [] and len(handles) == 1

    ok, shown, created, closed, _ = run_guard(maintenance_sequence=(True, False))
    assert ok is False and shown == ["update.startup.maintenance"] and created == [] and closed == []

    # 创建应用互斥锁之后发现维护锁激活时，必须关闭已创建句柄
    ok, shown, created, closed, handles = run_guard(maintenance_sequence=(False, True))
    assert ok is False and shown == ["update.startup.maintenance"]
    assert len(created) == 1 and len(closed) == 1 and handles == []

    ok, shown, _, _, _ = run_guard(pending=True)
    assert ok is False and shown == ["update.startup.repair"]

    ok, shown, _, _, _ = run_guard(fail=True)
    assert ok is False and shown == ["update.startup.failed"]
