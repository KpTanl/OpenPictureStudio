"""图像计算设置、GPU 自检结果持久化与设置页 GPU 状态展示。"""

from PySide6.QtCore import QSettings
import pytest

from src.application.gpu_backend import GpuStatus
from src.application.gpu_qualification import (
    qualification_key,
    store_qualification,
    stored_qualification,
)
from src.application.image_compute_settings import ImageComputeSettings, MIB
from src.application.memory_settings import GIB
from src.application.storage_paths import data_root
from src.i18n import TranslationService, set_translation_service
from src.ui.dialogs.settings_dialog import ComputeStatus, SettingsDialog, SettingsValues


def test_image_compute_settings_validation_roundtrip_and_cache_budget(tmp_path):
    default = ImageComputeSettings()
    assert (default.engine, default.cache, default.cache_mib) == ("auto", "auto", 2048)

    for kwargs in (
        {"engine": "gpu"},
        {"cache": "unlimited"},
        {"cache_mib": 32},
        {"cache_mib": 65537},
        {"cache_mib": True},
        {"cache_mib": 1024.0},
    ):
        with pytest.raises(ValueError):
            ImageComputeSettings(**kwargs)

    assert ImageComputeSettings(cache="off").cache_bytes(8 * GIB) == 0
    assert ImageComputeSettings(cache="auto").cache_bytes(8 * GIB) == 2 * GIB
    assert ImageComputeSettings(cache="auto").cache_bytes(2 * GIB) == GIB
    assert ImageComputeSettings(cache="custom", cache_mib=512).cache_bytes(8 * GIB) == 512 * MIB
    assert ImageComputeSettings(cache="custom", cache_mib=4096).cache_bytes(2 * GIB) == GIB

    ini_path = tmp_path / "compute.ini"
    store = QSettings(str(ini_path), QSettings.Format.IniFormat)
    custom = ImageComputeSettings(engine="cpu", cache="custom", cache_mib=512)
    custom.save(store)
    reloaded = ImageComputeSettings.load(QSettings(str(ini_path), QSettings.Format.IniFormat))
    assert reloaded == custom

    corrupt = QSettings(str(tmp_path / "corrupt.ini"), QSettings.Format.IniFormat)
    corrupt.setValue("engine", "invalid")
    corrupt.setValue("cache_mib", "bad")
    corrupt.sync()
    assert ImageComputeSettings.load(corrupt) == default


def test_gpu_qualification_storage_roundtrip_and_invalidation():
    adapter = {"name": "Adapter X", "vendor": 0x10DE, "device": 0x2684, "driver": "555.12"}
    key = qualification_key(adapter, "dxc")
    assert set(key) >= {"name", "vendor", "device", "driver", "revision", "compiler", "application"}

    assert stored_qualification(key) is None
    qual = {"max_lsb": 1, "cpu_ms": 5.2, "gpu_ms": 1.1}
    inputs = frozenset({("pixels", 3, 8, "little", "srgb"), ("png-filtered", 4, 8, "little", "srgb")})

    for denoise_passed in (True, False):
        store_qualification(key, qual, inputs, denoise_passed)
        loaded = stored_qualification(key)
        assert loaded == (qual, inputs, denoise_passed)

    # 驱动、修订号或应用版本任一变化都应使旧自检失效
    assert stored_qualification({**key, "driver": "556.00"}) is None
    assert stored_qualification({**key, "revision": key["revision"] + "-old"}) is None
    assert stored_qualification({**key, "application": "0.16.1"}) is None

    # 异常数据：max_lsb 超标、denoise 非布尔、JSON 损坏均安全返回 None
    store_qualification(key, {"max_lsb": 2, "cpu_ms": 5.0, "gpu_ms": 2.0}, inputs, True)
    assert stored_qualification(key) is None

    store_qualification(key, qual, inputs, 1)
    assert stored_qualification(key) is None

    path = data_root() / "gpu" / "qualification.json"
    path.write_text("{broken json", encoding="utf-8")
    assert stored_qualification(key) is None


def test_settings_dialog_projects_gpu_status_and_denoise_feature(qapp):
    translator = TranslationService(default_language="en")
    set_translation_service(translator)
    tr = translator.translate
    current_status = ComputeStatus(gpu=GpuStatus(state="disabled", reason="cpu_selected"))
    dialog = SettingsDialog(
        values=SettingsValues(image_compute=ImageComputeSettings()),
        page="performance",
        compute_status=lambda: current_status,
    )
    try:
        dialog.show()
        assert dialog.gpu_status_row.description() == tr("settings.compute.status.disabled")

        current_status = ComputeStatus(gpu=GpuStatus(state="unavailable", reason="no_adapter"))
        dialog.retranslate_ui()
        assert dialog.gpu_status_row.description() == tr("settings.compute.status.no_adapter")

        base_caps = {
            "source_inputs": (("pixels", 3, 8, "little", "srgb"),),
            "geometry": True,
            "color": True,
            "face_analysis": (),
            "tools": (),
            "driver": "555.12",
        }
        gpu_ready = GpuStatus(state="ready", tier="A", name="RTX Test", device="dxgi:0")
        denoise_feature = tr("settings.compute.feature.denoise")
        color_feature = tr("settings.compute.feature.color")

        current_status = ComputeStatus(gpu=gpu_ready, capabilities={**base_caps, "denoise": True})
        dialog.retranslate_ui()
        text_with_denoise = dialog.gpu_status_row.description()
        assert color_feature in text_with_denoise and denoise_feature in text_with_denoise

        current_status = ComputeStatus(gpu=gpu_ready, capabilities={**base_caps, "denoise": False})
        dialog.retranslate_ui()
        text_without_denoise = dialog.gpu_status_row.description()
        assert color_feature in text_without_denoise and denoise_feature not in text_without_denoise
    finally:
        dialog.close()
