from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass
from io import BytesIO

from PIL import Image


OBSERVATION_IMAGE_LIMIT = 9
REQUEST_IMAGE_LIMIT = 24
REQUEST_IMAGE_BYTES = 64 * 1024 * 1024
MIN_REGION_SIDE = 128


@dataclass(frozen=True)
class ImageSizingRule:
    long_edge: int
    patches: int = 0
    short_edge: int = 0
    max_patches: int = 30000
    multiplier: float = 1.2
    base_tokens: int = 0
    tile_tokens: int = 0
    basis: str = "openai.patch32"
    native_known: bool = True

    @property
    def native_long_edge(self):
        if not self.native_known:
            return 0
        limits = [self.long_edge]
        limits.extend(limit for limit in (self.short_edge,) if limit)
        limits.extend(32 * math.isqrt(limit) for limit in (self.patches, self.max_patches) if limit)
        return min(limits)


@dataclass(frozen=True)
class ImageModelRule:
    default_detail: str
    details: dict[str, ImageSizingRule]


IMAGE_MODEL_RULES = {
    **{name: ImageModelRule("high", {
        "low": ImageSizingRule(512, max_patches=0, base_tokens=base, basis="openai.tile512"),
        "high": ImageSizingRule(2048, short_edge=768, max_patches=0, base_tokens=base,
                                tile_tokens=tile, basis="openai.tile512")})
       for name, (base, tile) in {"gpt-4o": (85, 170), "gpt-4.1": (85, 170),
           "gpt-4o-mini": (2833, 5667), "gpt-5.1": (70, 140), "gpt-5": (70, 140),
           "o1": (75, 150), "o1-pro": (75, 150), "o3": (75, 150)}.items()},
    **{name: ImageModelRule("original", {
        "low": ImageSizingRule(512),
        "high": ImageSizingRule(65535 if name == "gpt-6-astra" else 2048, patches=2500),
        "original": ImageSizingRule(65535)})
       for name in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")},
    "gpt-5.5": ImageModelRule("original", {
        "low": ImageSizingRule(512), "high": ImageSizingRule(2048, patches=2500),
        "original": ImageSizingRule(6000, patches=10000)}),
    **{name: ImageModelRule("high", {
        "low": ImageSizingRule(2048, patches=6144), "high": ImageSizingRule(2048, patches=2500),
        "original": ImageSizingRule(6000, patches=10000)})
       for name in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano")},
    **{name: ImageModelRule("high", {
        detail: ImageSizingRule(2048, patches=6144, multiplier=multiplier) for detail in ("low", "high")})
       for name, multiplier in {"gpt-5.2": 1.2, "gpt-4.1-mini": 1.62}.items()},
    **{name: ImageModelRule("high", {
        detail: ImageSizingRule(2048, patches=6144, multiplier=multiplier,
                               basis="pixels.heuristic", native_known=False) for detail in ("low", "high")})
       for name, multiplier in {"gpt-4.1-nano": 2.46, "gpt-5-mini": 1.2,
                               "gpt-5-nano": 1.5, "o4-mini": 1.72}.items()},
}


def image_sizing_rule(model):
    name = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model.model.strip())
    known = IMAGE_MODEL_RULES.get(name)
    if known is None:
        return None
    detail = model.image_detail or "auto"
    selected = known.default_detail if detail == "auto" else detail
    if selected not in known.details:
        if not any(rule.native_known for rule in known.details.values()):
            return None
        raise ImageObservationError("AI_IMAGE_DETAIL", detail=detail, supported=["auto", *known.details])
    return known.details[selected]


def image_capability(model):
    rule = image_sizing_rule(model)
    configured = model.image_effective_long_edge
    limit = rule.native_long_edge if rule else 0
    return {"configured_long_edge": configured,
            "effective_long_edge": min(configured, limit) if limit else configured,
            "rule_long_edge": limit, "basis": rule.basis if rule else "manual",
            "detail": model.image_detail or "service_default"}


def estimate_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    wide = sum(ord(char) > 127 for char in text)
    return max(1, (len(text) - wide + 3) // 4 + wide)


class ImageMemory:
    def __init__(self, resources):
        self.resources = resources
        self.holds = []

    def resize(self, size, is_cancelled):
        block = 8 * 1024 * 1024
        count = math.ceil(size / block)
        previous = len(self.holds)
        try:
            while len(self.holds) < count:
                self.holds.append(self.resources.hold_memory(block, is_cancelled))
        except BaseException:
            while len(self.holds) > previous:
                self.holds.pop().close()
            raise
        while len(self.holds) > count:
            self.holds.pop().close()

    def close(self):
        while self.holds:
            self.holds.pop().close()


class ImageObservationError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.details = details


def jpeg_information(data):
    with Image.open(BytesIO(data)) as image:
        return {"size": list(image.size), "bytes": len(data)}


def preview_size(size, edge):
    scale = min(1.0, edge / max(size))
    return [max(1, round(value * scale)) for value in size]


def image_token_estimate(model, information):
    width, height = information["size"]
    rule = image_sizing_rule(model)
    if rule is None:
        return math.ceil(width / 32) * math.ceil(height / 32) * 2, "pixels.heuristic"
    width, height = preview_size((width, height), rule.long_edge)
    if rule.basis == "openai.tile512":
        if not rule.tile_tokens:
            return rule.base_tokens, rule.basis
        scale = min(1.0, rule.short_edge / min(width, height))
        width, height = max(1, math.floor(width * scale)), max(1, math.floor(height * scale))
        return rule.base_tokens + rule.tile_tokens * math.ceil(width / 512) * math.ceil(height / 512), rule.basis
    count = math.ceil(width / 32) * math.ceil(height / 32)
    if rule.patches and count > rule.patches:
        scale = math.sqrt(32 * 32 * rule.patches / (width * height))
        scale *= min(max(1, math.floor(width * scale / 32)) / (width * scale / 32),
                     max(1, math.floor(height * scale / 32)) / (height * scale / 32), 1)
        count = math.ceil(max(1, math.floor(width * scale)) / 32) * math.ceil(max(1, math.floor(height * scale)) / 32)
    return math.ceil(count * rule.multiplier), rule.basis


def described_image(data, model, **metadata):
    info = {**jpeg_information(data), **metadata}
    tokens, basis = image_token_estimate(model, info)
    return {**info, "estimated_tokens": tokens, "estimate_basis": basis}


def image_part(protocol, data, detail="", information=None, *, encode=True):
    url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii") if encode else ""
    part = ({"type": "input_image", "image_url": url} if protocol == "responses"
            else {"type": "image_url", "image_url": {"url": url}})
    if detail:
        if protocol == "responses":
            part["detail"] = detail
        else:
            part["image_url"]["detail"] = detail
    if information is not None:
        part["_ops_image"] = information
    return part


def public_image_parts(items):
    for item in items:
        item.pop("_ops_group", None)
        if isinstance(item.get("content"), list):
            for part in item["content"]:
                part.pop("_ops_image", None)
    return items


@dataclass
class _ImageSlot:
    information: dict
    group: str
    protected: bool
    owner: str
    index: int
    message: dict | None = None


def constrain_images(context, images, model, estimate, *, evidence=()):
    metadata = context.setdefault("image_info", {})
    pinned = context.get("pinned_images", {})
    slots = []
    owners = list(dict.fromkeys([message["id"] for message in context.get("messages", ()) if message["id"] in images] + list(images)))
    for owner in owners:
        attached = images[owner]
        if owner not in metadata:
            metadata[owner] = [jpeg_information(data) for data in attached]
        information = metadata[owner]
        for index, info in enumerate(information):
            protected = index in pinned.get(owner, ()) or (
                "pinned_images" not in context and owner not in context.get("history_ids", ()) and index == 0)
            slots.append(_ImageSlot(info, "base:" + owner + ":" + str(info.get("group", index)),
                                    protected, owner, index))
    for message in (*context.get("agent_items", ()), *evidence):
        for index, part in enumerate(message.get("content", ()) if isinstance(message.get("content"), list) else ()):
            if "_ops_image" in part:
                info = part["_ops_image"]
                slots.append(_ImageSlot(info, str(message["_ops_group"]),
                                        message["_ops_group"] in context.get("protected_tool_groups", ()),
                                        "tool", index, message))
    retained = list(slots)
    original_images = dict(images)
    original_metadata = dict(metadata)

    def usage():
        incoming = sum(image_token_estimate(model, slot.information)[0] for slot in retained)
        return {"count": len(retained), "bytes": sum(slot.information["bytes"] for slot in retained),
                "image_tokens": incoming, "estimated_tokens": estimate(),
                "max_count": REQUEST_IMAGE_LIMIT, "max_bytes": REQUEST_IMAGE_BYTES,
                "context_window": model.context_window}

    def fits(current):
        return (current["count"] <= REQUEST_IMAGE_LIMIT and current["bytes"] <= REQUEST_IMAGE_BYTES
                and (not model.context_window or current["estimated_tokens"]
                     + model.max_output_tokens + model.reasoning_reserve <= model.context_window))

    protected_groups = {slot.group for slot in retained if slot.protected}
    groups = list(dict.fromkeys(slot.group for slot in retained if slot.group not in protected_groups))

    def remove(removed):
        removed_ids = {id(slot) for slot in removed}
        retained[:] = [slot for slot in retained if id(slot) not in removed_ids]
        for owner in {slot.owner for slot in removed if slot.message is None}:
            kept = {slot.index for slot in retained if slot.owner == owner and slot.message is None}
            images[owner] = [data for index, data in enumerate(original_images[owner]) if index in kept]
            metadata[owner] = [info for index, info in enumerate(original_metadata[owner]) if index in kept]
        messages = {id(slot.message): slot.message for slot in removed if slot.message is not None}
        for message in messages.values():
            text_kind = message["content"][0]["type"]
            message["content"] = [{"type": text_kind, "text": "Earlier image evidence is no longer attached. Its tool result remains available."}]

    current = usage()
    while not fits(current) and groups:
        group = groups.pop(0)
        remove([slot for slot in retained if slot.group == group])
        current = usage()
    history_ids = list(context.get("history_ids", ()))
    while not fits(current) and history_ids:
        removed = {history_ids.pop(0)}
        while history_ids:
            following = next(message for message in context["messages"] if message["id"] == history_ids[0])
            if following["role"] == "user":
                break
            removed.add(history_ids.pop(0))
        remove([slot for slot in retained if slot.message is None and slot.owner in removed])
        context["messages"] = [message for message in context["messages"] if message["id"] not in removed]
        for owner in removed:
            images.pop(owner, None)
            metadata.pop(owner, None)
        context["history_ids"] = history_ids
        context["omitted_messages"] = context.get("omitted_messages", 0) + len(removed)
        current = usage()
    if not fits(current):
        image_limit = current["count"] > REQUEST_IMAGE_LIMIT or current["bytes"] > REQUEST_IMAGE_BYTES
        raise ImageObservationError("AI_IMAGE_BUDGET" if image_limit else "AI_CONTEXT_BUDGET", **current)
    context["image_budget"] = current
    if "manifest" in context:
        context["manifest"] = [item for item in context["manifest"] if item["kind"] not in ("image", "history")]
        if history_ids:
            context["manifest"].insert(0, {"kind": "history", "count": len(history_ids),
                                           "omitted": context.get("omitted_messages", 0)})
        if retained:
            context["manifest"].append({"kind": "image", "count": current["count"], "bytes": current["bytes"]})
    context["sent_images"] = [{**slot.information,
                              "estimated_tokens": image_token_estimate(model, slot.information)[0],
                              "estimate_basis": image_token_estimate(model, slot.information)[1]} for slot in retained]
    return current["estimated_tokens"]
