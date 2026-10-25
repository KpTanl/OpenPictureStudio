from __future__ import annotations

from PIL import Image


def render_tool(tool_id: str, image: Image.Image, params: dict, *, background_remover=None, sampling=None) -> Image.Image | None:
    if tool_id == "ai_enhance":
        from .ai_enhancer import AIEnhancer, AIEnhanceConfig

        mode = {"auto": "portrait", "sharpen": "portrait", "deblur": "superres"}.get(
            params.get("mode", "auto"), params.get("mode", "portrait"),
        )
        factor = int(str(params.get("upscale", "1x")).rstrip("xX"))
        result = AIEnhancer().enhance(image, AIEnhanceConfig(
            mode=mode, strength=int(params.get("strength", 50)), upscale_factor=factor,
        ))
        if result is not None and image.mode == "RGBA":
            result = result.convert("RGBA")
            result.putalpha(image.getchannel("A").resize(result.size, Image.Resampling.LANCZOS))
        if result is not None and sampling is not None and sampling.active:
            sampling.resize(result.size)
        return result
    if tool_id == "bg_remove":
        from .bg_remover import BackgroundRemover

        remover = background_remover if background_remover is not None else BackgroundRemover()
        if params.get("mode", "ai") == "ai":
            options = {"sampling": sampling} if sampling is not None and sampling.active else {}
            cutout = remover.remove_by_ai(image, **options)
        else:
            cutout = remover.remove_by_color(
                image, tuple(params.get("target_color", (255, 255, 255))),
                params.get("tolerance", 30),
                **({"sampling": sampling} if sampling is not None and sampling.active else {}),
            )
        if cutout is not None and params.get("edge_radius", 2) > 0:
            cutout = remover.optimize_edges(cutout, radius=params.get("edge_radius", 2),
                **({"sampling": sampling} if sampling is not None and sampling.active else {}))
        background = params.get("bg_type", "transparent")
        if cutout is None or background == "transparent":
            return cutout
        color = (0, 0, 0) if background == "black" else (255, 255, 255)
        result = Image.new("RGB", cutout.size, color)
        result.paste(cutout, (0, 0), cutout)
        return result
    if tool_id == "id_photo":
        from .tools.id_photo import BG_PRESETS, ID_PHOTO_PARAMETERS

        checked = ID_PHOTO_PARAMETERS.normalize(params)
        if not checked.success:
            raise ValueError(checked.error.message_key)
        params = checked.data
        result = image.resize((params["custom_width"], params["custom_height"]), Image.Resampling.LANCZOS)
        if sampling is not None and sampling.active:
            sampling.resize(result.size)
        color = BG_PRESETS.get(params.get("bg_preset", "白色"))
        if color is None:
            return result
        background = Image.new("RGBA", result.size, (*color, 255))
        return Image.alpha_composite(background, result.convert("RGBA")).convert("RGB")
    if tool_id == "shape_crop":
        from .shape_cropper import ShapeCropper, CropRegion

        return ShapeCropper(image).apply_region(CropRegion(
            id=1, rect=tuple(params.get("rect", (0.15, 0.15, 0.7, 0.7))),
            shape=params.get("shape", "circle"), corner_radius=params.get("corner_radius", 20),
        ), sampling=sampling)
    if tool_id == "lasso":
        from .lasso_tool import LassoTool

        selections = list(params.get("regions", []))
        if len(params.get("draft_points", [])) >= 3:
            selections.append({"points": params["draft_points"], "op": "add"})
        regions = [{"points": [(int(x * image.width), int(y * image.height))
                                for x, y in region["points"]],
                    "op": region.get("op", "add")}
                   for region in selections]
        if not regions:
            return image
        return LassoTool().cutout_by_regions(
            image, regions, keep_inside=params.get("keep_inside", True),
            feather_radius=params.get("feather", 0),
            sampling=sampling,
        )
    raise ValueError(tool_id)
