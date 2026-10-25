from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QSignalBlocker, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from resources.styles.semantic_colors import MIXER_CHANNEL_COLORS, mixer_track_colors
from resources.styles.theme_variables import CONTROL_HEIGHT_COMPACT, SPACING_MD, SPACING_SM, SPACING_XS, px
from .apple_button import AppleCompactButton
from .parameter_row import ParameterRow, ParameterSpec
from ...core.mixer_sampling import hue_of_rgb8, nearest_mixer_channel


_MIXER_CHANNELS = [
    ("red", "color_adjust.mixer.red", "Red"),
    ("orange", "color_adjust.mixer.orange", "Orange"),
    ("yellow", "color_adjust.mixer.yellow", "Yellow"),
    ("green", "color_adjust.mixer.green", "Green"),
    ("cyan", "color_adjust.mixer.cyan", "Cyan"),
    ("blue", "color_adjust.mixer.blue", "Blue"),
    ("purple", "color_adjust.mixer.purple", "Purple"),
    ("magenta", "color_adjust.mixer.magenta", "Magenta"),
]
assert [channel for channel, _, _ in _MIXER_CHANNELS] == list(MIXER_CHANNEL_COLORS)

_MIXER_SLIDERS = (
    ParameterSpec("hue", "color_adjust.mixer.hue", "Hue", -100, 100, 0, "int_signed"),
    ParameterSpec("sat", "color_adjust.mixer.sat", "Saturation", -100, 100, 0, "int_signed"),
    ParameterSpec("lum", "color_adjust.mixer.lum", "Luminance", -100, 100, 0, "int_signed"),
)

# 各颜色与分量在改动说明里的名称和读数格式，按 color_mixer.颜色 与 color_mixer.分量 声明，供面板的 PARAMETER_LABELS 使用
MIXER_PARAMETER_LABELS = {
    **{"color_mixer." + key: (label_key, "") for key, label_key, _ in _MIXER_CHANNELS},
    **{"color_mixer." + spec.key: (spec.label_key, spec.value_format) for spec in _MIXER_SLIDERS},
}


def hue_of_rgb(red: int, green: int, blue: int) -> float:
    return hue_of_rgb8(red, green, blue)


class ColorMixerWidget(QWidget):
    """8 色通道色彩混合器控件 (HSL Mixer)"""

    values_changed = pyqtSignal()
    eyedropper_toggled = pyqtSignal(bool)

    def __init__(self, tr_fn, parent=None):
        super().__init__(parent)
        self._tr = tr_fn
        self._current_channel = "red"
        self._eyedropper_state = "closed"
        self._eyedropper_available = True
        self._values: dict[str, dict[str, int]] = {
            ch[0]: {"hue": 0, "sat": 0, "lum": 0} for ch in _MIXER_CHANNELS
        }
        self._channel_buttons: dict[str, QPushButton] = {}
        self._rows: dict[str, ParameterRow] = {}
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(px(SPACING_MD))
        layout.setContentsMargins(0, 0, 0, 0)

        # 8色选择行
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(px(SPACING_SM))
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for key, i18n_k, fallback in _MIXER_CHANNELS:
            btn = QPushButton()
            btn.setObjectName("mixerChannelDot")
            btn.setProperty("channel", key)
            btn.setCheckable(True)
            btn.setChecked(key == "red")
            btn.setFixedSize(CONTROL_HEIGHT_COMPACT, CONTROL_HEIGHT_COMPACT)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(self._tr(i18n_k, fallback))
            btn.clicked.connect(lambda checked, ch=key: self._on_channel_selected(ch))
            btn_layout.addWidget(btn)
            self._channel_buttons[key] = btn
        layout.addLayout(btn_layout)

        # 当前通道标题与单通道重置 / 吸管
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(px(SPACING_SM))
        self._ch_title_label = QLabel(self._tr("color_adjust.mixer.red", "Red"))
        self._ch_title_label.setObjectName("mixerChannelTitle")
        header_row.addWidget(self._ch_title_label)
        header_row.addStretch()

        self._btn_eyedropper = AppleCompactButton(self._tr("color_adjust.mixer.eyedropper", "Pick Color"))
        self._btn_eyedropper.setCheckable(True)
        self._btn_eyedropper.setToolTip(self._tr("color_adjust.mixer.eyedropper_tip", "Click the photo to select the channel of its color before the mixer"))
        self._btn_eyedropper.toggled.connect(self._on_eyedropper_toggled)
        header_row.addWidget(self._btn_eyedropper)

        self._btn_reset_ch = AppleCompactButton(self._tr("color_adjust.mixer.reset_channel", "Reset Channel"))
        self._btn_reset_ch.clicked.connect(self._on_reset_current_channel)
        header_row.addWidget(self._btn_reset_ch)
        layout.addLayout(header_row)

        # 色相、饱和度、明度：滑轨颜色随当前通道变化
        rows = QVBoxLayout()
        rows.setSpacing(px(SPACING_XS))
        for spec in _MIXER_SLIDERS:
            row = ParameterRow(spec, self._tr(spec.label_key, spec.label_fallback), self)
            row.value_edited.connect(lambda value, key=spec.key: self._on_slider_changed(key, value))
            rows.addWidget(row)
            self._rows[spec.key] = row
        layout.addLayout(rows)
        self._sync_sliders_from_values()

    def edit_controls(self):
        return tuple(row.slider for row in self._rows.values())

    def retranslate_ui(self):
        for key, i18n_k, fallback in _MIXER_CHANNELS:
            if key == self._current_channel:
                self._ch_title_label.setText(self._tr(i18n_k, fallback))
            self._channel_buttons[key].setToolTip(self._tr(i18n_k, fallback))
        self._update_eyedropper_text()
        self._btn_reset_ch.setText(self._tr("color_adjust.mixer.reset_channel", "Reset Channel"))
        for row in self._rows.values():
            row.set_label(self._tr(row.spec.label_key, row.spec.label_fallback))

    def _on_eyedropper_toggled(self, checked: bool):
        self.eyedropper_toggled.emit(checked)

    def set_eyedropper_active(self, active: bool) -> None:
        self._btn_eyedropper.setChecked(active)

    def set_eyedropper_state(self, state: str) -> None:
        self._eyedropper_state = state
        with QSignalBlocker(self._btn_eyedropper):
            self._btn_eyedropper.setChecked(state != "closed")
        self._update_eyedropper_text()

    def set_eyedropper_available(self, available: bool) -> None:
        self._eyedropper_available = available
        self._btn_eyedropper.setEnabled(available)
        self._update_eyedropper_text()

    def _update_eyedropper_text(self):
        key = "color_adjust.mixer.preparing" if self._eyedropper_state == "preparing" else "color_adjust.mixer.eyedropper"
        self._btn_eyedropper.setText(self._tr(key))
        tip = "color_adjust.mixer.eyedropper_tip" if self._eyedropper_available else "color_adjust.mixer.unavailable"
        self._btn_eyedropper.setToolTip(self._tr(tip))

    def select_channel(self, channel_id: int) -> str:
        channel = _MIXER_CHANNELS[channel_id][0]
        self._on_channel_selected(channel)
        return channel

    def select_channel_for_hue(self, hue_deg: float) -> str:
        return self.select_channel(nearest_mixer_channel(hue_deg))

    def _on_channel_selected(self, ch: str):
        self._current_channel = ch
        for key, btn in self._channel_buttons.items():
            btn.setChecked(key == ch)
        for key, i18n_k, fallback in _MIXER_CHANNELS:
            if key == ch:
                self._ch_title_label.setText(self._tr(i18n_k, fallback))
                break
        self._sync_sliders_from_values()

    def _sync_sliders_from_values(self):
        """把当前通道的数值与滑轨颜色投影到三条参数行；不发出用户编辑信号。"""
        tracks = mixer_track_colors(self._current_channel)
        for key, row in self._rows.items():
            row.slider.set_track_colors(tracks[key])
            row.set_value(int(self._values[self._current_channel].get(key, 0)))

    def _on_slider_changed(self, key: str, val: int):
        self._values[self._current_channel][key] = val
        self._rows[key].set_value(val)
        self.values_changed.emit()

    def _on_reset_current_channel(self):
        self._values[self._current_channel] = {"hue": 0, "sat": 0, "lum": 0}
        self._sync_sliders_from_values()
        self.values_changed.emit()

    def get_values(self) -> dict:
        return {ch: dict(vals) for ch, vals in self._values.items()}

    @staticmethod
    def _clamp_mixer_val(v) -> int:
        try:
            f = float(v)
            if not math.isfinite(f):
                return 0
            return int(max(-100, min(100, round(f))))
        except (ValueError, TypeError):
            return 0

    def set_values(self, mixer_data: dict | None):
        if not mixer_data:
            self._values = {ch[0]: {"hue": 0, "sat": 0, "lum": 0} for ch in _MIXER_CHANNELS}
        else:
            for ch in self._values:
                if ch in mixer_data and isinstance(mixer_data[ch], dict):
                    self._values[ch] = {
                        "hue": self._clamp_mixer_val(mixer_data[ch].get("hue", 0)),
                        "sat": self._clamp_mixer_val(mixer_data[ch].get("sat", 0)),
                        "lum": self._clamp_mixer_val(mixer_data[ch].get("lum", 0)),
                    }
                else:
                    self._values[ch] = {"hue": 0, "sat": 0, "lum": 0}
        self._sync_sliders_from_values()

    def reset_all(self):
        self.set_values(None)
