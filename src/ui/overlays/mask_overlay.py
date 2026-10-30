from __future__ import annotations

import copy
import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen, QTransform

from resources.styles.semantic_colors import OVERLAY_COLORS
from ...core.local_masks import default_mask
from ...core.mask_components import (COMPONENT_LIMIT, NODE_COMPONENT_LIMIT, POINT_LIMIT, STROKE_KEYS, brush_strokes,
                                     component_from_shape, new_mask)
from ..theme import to_qcolor
from .base_overlay import ViewerOverlay


class MaskOverlay(ViewerOverlay):
    selected = Signal(object)
    gesture_started = Signal(object)
    gesture_changed = Signal(object)
    gesture_finished = Signal(object)
    gesture_cancelled = Signal()
    creation_started = Signal(str, str)
    creation_finished = Signal()
    constraint_reached = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._masks, self._selected = [], None
        self._reference_size = (1, 1)
        self._enabled, self._visible = False, True
        self._creation = None
        self._drag = None
        self._candidate = None
        self._blank_press = None
        self._range_image, self._range_hold = None, None
        self._parents, self._component_mode = {}, False
        self._creation_parent, self._creation_op = None, "add"
        self._brush_settings = {}
        self._paint_target = None
        self._hover = None
        self._point_total = 0
        self._stroke_point_limit = 0
        self._parent_masks = {}
        self._component_count = 0

    def set_viewer(self, viewer):
        if self._viewer is not None:
            self._viewer.removeEventFilter(self)
        super().set_viewer(viewer)
        if viewer is not None:
            viewer.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide)
                or event.type() == QEvent.Type.UngrabMouse and self._drag is not None):
            self.cancel_interaction()
        elif event.type() == QEvent.Type.Leave and self._hover is not None:
            self._hover = None
            self.update_view()
        return False

    def set_state(self, masks, selected, reference_size, *, enabled, component=None, composite=False):
        self._component_mode, self._parents = composite, {}
        if composite:
            self._masks = []
            self._parent_masks = {mask["id"]: mask for mask in masks or ()}
            self._component_count = sum(len(mask["components"]) for mask in masks or ())
            self._point_total = 0
            for mask in masks or ():
                for part in mask["components"]:
                    self._parents[part["id"]] = mask
                    if part["type"] in ("linear", "radial"):
                        self._masks.append(copy.deepcopy(part))
                    elif part["type"] == "brush":
                        self._point_total += sum(len(stroke["points"]) for stroke in brush_strokes(part))
            selected = component
        else:
            self._masks = copy.deepcopy(masks or [])
        self._selected, self._reference_size, self._enabled = selected, reference_size, enabled
        self.update_view()

    def set_visible(self, visible):
        self._visible = visible
        if not visible:
            self.cancel_interaction()
        self.update_view()

    def set_range(self, image=None, hold=None):
        previous = self._range_hold
        self._range_image, self._range_hold = image, hold
        if previous is not None:
            previous.close()
        self.update_view()

    def begin_creation(self, kind, name, *, parent=None, operation="add", brush=None, target=None, mask_name=None):
        """在画布上新建一块区域；画笔可以连续涂多笔，都归进同一块画笔区域（target 是接着涂的那一块）。"""
        self.cancel_interaction()
        self._creation = kind, name
        self._creation_parent = (copy.deepcopy(parent) if parent is not None
                                 else new_mask(mask_name or name) if self._component_mode else None)
        self._creation_op = operation
        self._paint_target = target
        if brush is not None:
            self._brush_settings = dict(brush)
        self.set_cursor(Qt.CursorShape.CrossCursor)
        if kind == "brush" and self._viewer is not None:
            self._viewer.setMouseTracking(True)
        self.creation_started.emit(kind, operation)

    @property
    def creation(self):
        """正在画布上新建的区域：(类型, 添加或减去)；没有时为 None。"""
        return None if self._creation is None else (self._creation[0], self._creation_op)

    def _payload(self, shape):
        if not self._component_mode:
            return copy.deepcopy(shape)
        if self._creation is not None:
            parent = self._parent_masks.get(self._creation_parent["id"], self._creation_parent)
        else:
            parent = self._parents.get(shape["id"])
        parent = copy.deepcopy(parent) if parent is not None else new_mask(shape["name"])
        part = component_from_shape(shape, shape.get("op", self._creation_op if self._creation is not None else "add"))
        index = next((i for i, item in enumerate(parent["components"]) if item["id"] == part["id"]), None)
        if index is None:
            parent["components"].append(part)
        else:
            parent["components"][index] = part
        return parent

    def is_dragging(self):
        return self._drag is not None

    @property
    def selected_component(self):
        return self._selected if self._component_mode else None

    def set_brush_settings(self, settings):
        self._brush_settings = dict(settings)
        self.update_view()

    def cancel_drag(self):
        """只停下拖到一半的这一下；正在新建的区域（如连续涂抹）照常继续。"""
        if self._drag is None:
            return
        self._drag, self._candidate = None, None
        self.gesture_cancelled.emit()
        self.drag_finished.emit()
        self.update_view()

    def cancel_interaction(self):
        active = self._drag is not None
        creating = self._creation is not None
        self._drag, self._candidate, self._creation = None, None, None
        self._blank_press, self._paint_target, self._hover = None, None, None
        if self._viewer is not None:
            self._viewer.setMouseTracking(False)
        if active:
            self.gesture_cancelled.emit()
            self.drag_finished.emit()
        if creating:
            self.creation_finished.emit()
        self.set_cursor(Qt.CursorShape.ArrowCursor)
        self.update_view()

    def _position(self, point, rect):
        return ((point.x() - rect.x()) / max(1, rect.width()),
                (point.y() - rect.y()) / max(1, rect.height()))

    def _screen(self, point, rect):
        return QPointF(rect.x() + point[0] * rect.width(), rect.y() + point[1] * rect.height())

    def _center(self, mask):
        if mask["type"] == "radial":
            return mask["center"]
        return [(a + b) / 2 for a, b in zip(mask["p0"], mask["p1"])]

    def _radial_point(self, mask, x, y):
        width, height = self._reference_size
        angle = math.radians(mask["angle"])
        cosine, sine = math.cos(angle), math.sin(angle)
        return (mask["center"][0] + (cosine * x - sine * y) / width,
                mask["center"][1] + (sine * x + cosine * y) / height)

    def _set_linear_span(self, mask, center, direction, half):
        unit = [0.0 if abs(component) < 1e-12 else component / extent
                for component, extent in zip(direction, self._reference_size)]
        for axis in (0, 1):
            if unit[axis] != 0:
                half = min(half, (center[axis] + 1) / abs(unit[axis]), (2 - center[axis]) / abs(unit[axis]))
        if 2 * half * math.hypot(*unit) <= 1e-6:
            return False
        mask["p0"] = [max(-1, min(2, center[axis] - unit[axis] * half)) for axis in (0, 1)]
        mask["p1"] = [max(-1, min(2, center[axis] + unit[axis] * half)) for axis in (0, 1)]
        return True

    def _handles(self, mask, rect):
        center = self._center(mask)
        result = {"move": self._screen(center, rect)}
        width, height = self._reference_size
        if mask["type"] == "linear":
            result.update(p0=self._screen(mask["p0"], rect), p1=self._screen(mask["p1"], rect))
            dx = (mask["p1"][0] - mask["p0"][0]) * width
            dy = (mask["p1"][1] - mask["p0"][1]) * height
            length = max(math.hypot(dx, dy), 1e-6)
            offset = 40 * width / max(1, rect.width())
            result["rotate"] = self._screen((center[0] - dy / length * offset / width,
                                             center[1] + dx / length * offset / height), rect)
        else:
            rx, ry = mask["radius"][0] * width, mask["radius"][1] * height
            for key, x, y in (("rx+", rx, 0), ("rx-", -rx, 0), ("ry+", 0, ry), ("ry-", 0, -ry),
                              ("feather", rx * (1 - mask["feather"]), 0),
                              ("rotate", rx + 32 * width / max(1, rect.width()), 0)):
                result[key] = self._screen(self._radial_point(mask, x, y), rect)
        return result

    def _hit(self, point, rect):
        current = next((mask for mask in self._masks if mask["id"] == self._selected), None)
        if current is not None:
            for key, location in reversed(tuple(self._handles(current, rect).items())):
                if math.hypot(point.x() - location.x(), point.y() - location.y()) <= 9:
                    return current, key
        for mask in self._masks:
            if mask["id"] == self._selected:
                continue
            location = self._screen(self._center(mask), rect)
            if math.hypot(point.x() - location.x(), point.y() - location.y()) <= 9:
                return mask, "move"
        if current is not None:
            position = self._position(point, rect)
            if current["type"] == "radial":
                width, height = self._reference_size
                dx, dy = (position[0] - current["center"][0]) * width, (position[1] - current["center"][1]) * height
                angle = math.radians(current["angle"])
                xx, yy = math.cos(angle) * dx + math.sin(angle) * dy, -math.sin(angle) * dx + math.cos(angle) * dy
                if (xx / (current["radius"][0] * width)) ** 2 + (yy / (current["radius"][1] * height)) ** 2 <= 1:
                    return current, "move"
            else:
                handles = self._handles(current, rect)
                direction = handles["p1"] - handles["p0"]
                length = max(math.hypot(direction.x(), direction.y()), 1e-6)
                for key in ("p0", "p1", "move"):
                    anchor = handles[key]
                    distance = abs((point.x() - anchor.x()) * direction.x() + (point.y() - anchor.y()) * direction.y()) / length
                    if distance <= 6:
                        return current, key
        return None, None

    def handle_mouse_press(self, event, image_rect):
        if not self._enabled or not self._visible or event.button() != Qt.MouseButton.LeftButton:
            return False
        if getattr(self._viewer, "space_pan_active", False):
            return False
        point = event.position()
        position = self._position(point, image_rect)
        if self._creation is not None:
            if not image_rect.contains(point.toPoint()):
                return False
            kind, name = self._creation
            if kind == "brush":
                mask = self._new_stroke(position, name)
                if mask is None:
                    return True
                handle = "brush"
            else:
                mask, handle = default_mask(kind, position, self._reference_size, name), "new"
        else:
            mask, handle = self._hit(point, image_rect)
        if mask is None:
            self._blank_press = QPointF(point)
            return False
        if self._creation is None:
            self.selected.emit((self._parents[mask["id"]]["id"], mask["id"]) if self._component_mode else mask["id"])
        self._drag = (copy.deepcopy(mask), handle, position, QPointF(point))
        self._candidate = copy.deepcopy(mask)
        self.drag_started.emit()
        self.gesture_started.emit(self._payload(mask))
        return True

    def _new_stroke(self, position, name):
        """按下画笔：在本次涂抹的画笔区域里加一笔，还没有这一块时新建；做不了时返回 None。"""
        settings = self._brush_settings
        if settings.get("opacity", 0) == 0:
            return None
        if self._point_total >= POINT_LIMIT:
            self.cancel_interaction()
            self.constraint_reached.emit("error.mask.point_limit")
            return None
        self._stroke_point_limit = POINT_LIMIT - self._point_total
        stroke = {"points": [list(position)], **{key: settings[key] for key in ("radius", "hardness", "opacity")}}
        parent = self._parent_masks.get(self._creation_parent["id"]) if self._creation_parent is not None else None
        existing = next((part for part in (parent or {}).get("components", ()) if part["id"] == self._paint_target), None)
        if existing is not None:
            mask = {key: copy.deepcopy(value) for key, value in existing.items() if key not in STROKE_KEYS}
            mask["strokes"] = [*copy.deepcopy(brush_strokes(existing)), stroke]
            return mask
        if (len((parent or {}).get("components", ())) >= COMPONENT_LIMIT
                or self._component_count >= NODE_COMPONENT_LIMIT):
            self.cancel_interaction()
            self.constraint_reached.emit("error.mask.component_limit")
            return None
        from uuid import uuid4
        self._paint_target = str(uuid4())
        return {"id": self._paint_target, "name": name, "enabled": True, "op": self._creation_op,
                "type": "brush", "strokes": [stroke]}

    def handle_mouse_move(self, event, image_rect):
        if self._drag is None:
            if self._creation is not None and self._creation[0] == "brush":
                self._hover = event.position()
                self.update_view()
            return False
        original, handle, start, screen_start = self._drag
        position = self._position(event.position(), image_rect)
        width, height = self._reference_size
        mask = copy.deepcopy(original)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if handle == "brush":
            self._hover = event.position()
            mask = copy.deepcopy(self._candidate)
            stroke = mask["strokes"][-1]
            point = [max(-1, min(2, value)) for value in position]
            previous = stroke["points"][-1]
            spacing = max(.25, stroke["radius"] * min(width, height) / 8)
            if math.hypot((point[0] - previous[0]) * width, (point[1] - previous[1]) * height) < spacing:
                self.update_view()
                return True
            if len(stroke["points"]) >= self._stroke_point_limit:
                self.cancel_interaction()
                self.constraint_reached.emit("error.mask.point_limit")
                return True
            stroke["points"].append(point)
        elif handle == "new":
            if math.hypot(event.position().x() - screen_start.x(), event.position().y() - screen_start.y()) < 3:
                return True
            if mask["type"] == "linear":
                mask.update(p0=list(start), p1=[max(-1, min(2, part)) for part in position])
            else:
                dx, dy = abs(position[0] - start[0]) * width, abs(position[1] - start[1]) * height
                if shift:
                    dx = dy = min(max(dx, dy), 2 * min(width, height))
                mask.update(center=list(start), radius=[max(1e-6, min(2, dx / width)), max(1e-6, min(2, dy / height))])
        elif handle == "move":
            keys = ("center",) if mask["type"] == "radial" else ("p0", "p1")
            delta = [position[axis] - start[axis] for axis in (0, 1)]
            for axis in (0, 1):
                delta[axis] = max(max(-1 - original[key][axis] for key in keys),
                                  min(min(2 - original[key][axis] for key in keys), delta[axis]))
            for key in keys:
                mask[key] = [original[key][axis] + delta[axis] for axis in (0, 1)]
        elif handle == "rotate":
            center = self._center(mask)
            before = math.atan2((start[1] - center[1]) * height, (start[0] - center[0]) * width)
            after = math.atan2((position[1] - center[1]) * height, (position[0] - center[0]) * width)
            delta = after - before
            if mask["type"] == "linear":
                dx, dy = (mask["p1"][0] - mask["p0"][0]) * width, (mask["p1"][1] - mask["p0"][1]) * height
                angle = math.atan2(dy, dx) + delta
                if shift:
                    angle = math.radians(round(math.degrees(angle) / 15) * 15)
                half = math.hypot(dx, dy) / 2
                if not self._set_linear_span(mask, center, (math.cos(angle), math.sin(angle)), half):
                    return True
            else:
                angle = mask["angle"] + math.degrees(delta)
                mask["angle"] = (round(angle / 15) * 15 if shift else angle) % 360
        elif mask["type"] == "linear":
            center = self._center(mask)
            dx, dy = (mask["p1"][0] - mask["p0"][0]) * width, (mask["p1"][1] - mask["p0"][1]) * height
            length = max(math.hypot(dx, dy), 1e-6)
            half = max(1e-3, abs(((position[0] - center[0]) * width * dx + (position[1] - center[1]) * height * dy) / length))
            if not self._set_linear_span(mask, center, (dx / length, dy / length), half):
                return True
        else:
            dx, dy = (position[0] - mask["center"][0]) * width, (position[1] - mask["center"][1]) * height
            angle = math.radians(mask["angle"])
            xx, yy = math.cos(angle) * dx + math.sin(angle) * dy, -math.sin(angle) * dx + math.cos(angle) * dy
            if handle == "feather":
                mask["feather"] = max(0, min(1, 1 - abs(xx) / (mask["radius"][0] * width)))
            else:
                axis = 0 if handle.startswith("rx") else 1
                radius = abs(xx if axis == 0 else yy)
                mask["radius"][axis] = max(1e-6, min(2, radius / (width if axis == 0 else height)))
                if shift:
                    radius = min(radius, 2 * min(width, height))
                    mask["radius"] = [max(1e-6, radius / width), max(1e-6, radius / height)]
        self._candidate = mask
        self.gesture_changed.emit(self._payload(mask))
        self.update_view()
        return True

    def handle_mouse_release(self, event, image_rect):
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._drag is None:
            if self._blank_press is not None:
                point, self._blank_press = self._blank_press, None
                if math.hypot(event.position().x() - point.x(), event.position().y() - point.y()) < 3:
                    self.selected.emit(None)
            return False
        if self._drag[1] == "brush":
            self.handle_mouse_move(event, image_rect)
            if self._drag is None:
                return True
        brush = self._drag[1] == "brush"
        component_id = self._candidate["id"]
        result = self._payload(self._candidate)
        self._selected = component_id
        self._drag, self._candidate = None, None
        self.gesture_finished.emit(result)
        self.drag_finished.emit()
        # 画笔松手后仍在涂抹，下一笔接着进同一块区域；渐变画完一个就结束
        if self._creation is not None and (not brush or not self._enabled or not self._visible):
            self._creation = None
            self._paint_target, self._hover = None, None
            if self._viewer is not None:
                self._viewer.setMouseTracking(False)
            self.set_cursor(Qt.CursorShape.ArrowCursor)
            self.creation_finished.emit()
        self.update_view()
        return True

    def handle_key_press(self, key):
        if key == Qt.Key.Key_Escape:
            if self._drag is not None or self._creation is not None:
                self.cancel_interaction()
                return True
            if self._selected is not None:
                self.selected.emit(None)
                return True
        return False

    def paint(self, painter, image_rect):
        if not self._visible or not self._enabled:
            return
        if self._range_image is not None:
            painter.drawImage(QRectF(image_rect), self._range_image)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["frame"]), 1))
        painter.setBrush(to_qcolor(OVERLAY_COLORS["handle"]))
        masks = [self._candidate if self._candidate is not None and mask["id"] == self._candidate["id"] else mask for mask in self._masks]
        if self._candidate is not None and not any(mask["id"] == self._candidate["id"] for mask in masks):
            masks.append(self._candidate)
        for mask in masks:
            if mask["type"] == "brush":
                # 正在画的这一笔半透明地跟手，松手后由范围图给出实际效果
                stroke = mask["strokes"][-1]
                painter.setBrush(Qt.BrushStyle.NoBrush)
                path = QPainterPath()
                path.moveTo(self._screen(stroke["points"][0], image_rect))
                for point in stroke["points"][1:]:
                    path.lineTo(self._screen(point, image_rect))
                radius = stroke["radius"] * min(self._reference_size)
                pen = QPen(to_qcolor(OVERLAY_COLORS["mask"]), 2 * radius * image_rect.width() / self._reference_size[0])
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                if len(stroke["points"]) == 1:
                    painter.drawPoint(self._screen(stroke["points"][0], image_rect))
                else:
                    painter.drawPath(path)
                painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["frame"]), 1))
                painter.setBrush(to_qcolor(OVERLAY_COLORS["handle"]))
                continue
            handles = self._handles(mask, image_rect)
            if mask["id"] != self._selected and (self._candidate is None or mask["id"] != self._candidate["id"]):
                painter.drawEllipse(handles["move"], 4, 4)
                continue
            if mask["type"] == "linear":
                delta = handles["p1"] - handles["p0"]
                length = max(math.hypot(delta.x(), delta.y()), 1e-6)
                normal = QPointF(-delta.y() / length, delta.x() / length) * math.hypot(image_rect.width(), image_rect.height())
                for key in ("p0", "move", "p1"):
                    painter.drawLine(handles[key] - normal, handles[key] + normal)
                painter.drawLine(handles["move"], handles["rotate"])
            else:
                width, height = self._reference_size
                rx, ry = mask["radius"][0] * width, mask["radius"][1] * height
                transform = QTransform().translate(handles["move"].x(), handles["move"].y())
                transform.scale(image_rect.width() / width, image_rect.height() / height)
                transform.rotate(mask["angle"])
                painter.setBrush(Qt.BrushStyle.NoBrush)
                for factor in (1, 1 - mask["feather"]):
                    path = QPainterPath()
                    path.addEllipse(QRectF(-rx * factor, -ry * factor, 2 * rx * factor, 2 * ry * factor))
                    painter.drawPath(transform.map(path))
                painter.setBrush(to_qcolor(OVERLAY_COLORS["handle"]))
                painter.drawLine(handles["rx+"], handles["rotate"])
            for point in handles.values():
                painter.drawEllipse(point, 4, 4)
        if self._hover is not None and self._creation is not None and self._creation[0] == "brush" and self._brush_settings:
            # 画笔大小与软边：外圈是笔刷范围，虚线内圈以内不透明度不减
            radius = self._brush_settings["radius"] * min(self._reference_size) * image_rect.width() / self._reference_size[0]
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["scrim"]), 3))
            painter.drawEllipse(self._hover, radius, radius)
            painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["frame"]), 1))
            painter.drawEllipse(self._hover, radius, radius)
            if self._brush_settings["hardness"] < 1:
                painter.setPen(QPen(to_qcolor(OVERLAY_COLORS["frame"]), 1, Qt.PenStyle.DashLine))
                inner = radius * max(.05, self._brush_settings["hardness"])
                painter.drawEllipse(self._hover, inner, inner)
        painter.restore()
