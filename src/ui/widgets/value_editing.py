"""数值控件共用的部分：用户编辑提交命令，以及读数的显示格式与解析。"""

from __future__ import annotations

import math


class ValueEditMixin:
    """数值控件的用户编辑提交命令；宿主类须提供 `value()`、`setValue()` 与 `valueEdited` 信号。"""

    def edit_value(self, value: int) -> None:
        previous = self.value()
        self.setValue(value)
        self._emit_value_edited(previous)

    def _emit_value_edited(self, previous: int) -> None:
        value = self.value()
        if value != previous:
            self.valueEdited.emit(value)


def format_slider_value(fmt: str, raw_value: int | float) -> str:
    """把滑块原始值显示为读数。

    fmt: "ev"（+0.56）、"kelvin"（5500K）、"int_signed"（+12）、"percent"（12 %）、"px"（12 px）、"int"（12）。
    """
    if fmt == "ev":
        return f"{raw_value / 100.0:+.2f}"
    if fmt == "kelvin":
        # 对数映射：raw=0 → 5500K（中性日光），raw=±100 → ×÷2 ≈ 2750K~11000K
        kelvin = int(round(5500 * (2.0 ** (raw_value / 100.0))))
        kelvin = max(2000, min(12000, kelvin))
        return f"{kelvin}K"
    if fmt == "int_signed":
        return f"{raw_value:+g}" if raw_value != 0 else "0"
    if fmt == "percent":
        return f"{raw_value} %"
    if fmt == "percent_tenths":
        return f"{raw_value / 10:g} %"
    if fmt == "px":
        return f"{raw_value} px"
    return f"{raw_value}"


def parse_slider_value(fmt: str, text: str, lo: int, hi: int) -> int | None:
    """把读数框里键入的文本解析回滑块原始值（format_slider_value 的逆）。

    解析失败返回 None（调用方据此还原显示）。结果按 [lo, hi] 截断。
    kelvin 走对数逆映射：raw = 100 * log2(kelvin / 5500)。
    """
    s = text.strip()
    if not s:
        return None
    try:
        if fmt == "ev":
            raw = int(round(float(s) * 100))
        elif fmt == "percent_tenths":
            raw = int(round(float(s.rstrip("%％").strip()) * 10))
        elif fmt == "kelvin":
            kelvin = max(2000.0, min(12000.0, float(s.lower().replace("k", "").strip())))
            raw = int(round(100.0 * math.log2(kelvin / 5500.0)))
        else:  # int_signed / percent / px / int / 其它
            if s.lower().endswith("px"):
                s = s[:-2]
            raw = int(round(float(s.rstrip("%％").strip())))
    except (ValueError, TypeError, OverflowError):
        return None
    return max(lo, min(hi, raw))
