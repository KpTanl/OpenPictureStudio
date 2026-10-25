from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageChops


@dataclass
class CropRegion:
    id: int
    rect: tuple[float, float, float, float]  # (x, y, w, h) in 0-1 ratios
    shape: str  # "rect", "circle", "ellipse", "round_rect"
    corner_radius: float = 0.0  # pixel radius for round_rect
    name: str = ""

    def to_pixel_rect(self, iw: int, ih: int) -> tuple[int, int, int, int]:
        """Convert normalized ratios to pixel coordinates (x, y, w, h)."""
        x, y, w, h = self.rect
        return (
            int(x * iw),
            int(y * ih),
            int(w * iw),
            int(h * ih),
        )


class ShapeCropper:
    def __init__(self, image: Image.Image):
        self.image = image

    # ── mask generation ────────────────────────────────────────────────

    def generate_mask(self, region: CropRegion,
                      size: tuple[int, int]) -> Image.Image:
        """Generate L-mode mask (255 inside shape, 0 outside)."""
        w, h = size
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)

        x, y, rw, rh = region.rect
        px, py = int(x * w), int(y * h)
        pw, ph = int(rw * w), int(rh * h)

        if pw < 2 or ph < 2:
            return mask

        if region.shape == "rect":
            draw.rectangle([px, py, px + pw - 1, py + ph - 1], fill=255)
        elif region.shape == "circle":
            d = min(pw, ph)
            cx, cy = px + pw // 2, py + ph // 2
            draw.ellipse([cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2], fill=255)
        elif region.shape == "ellipse":
            draw.ellipse([px, py, px + pw - 1, py + ph - 1], fill=255)
        elif region.shape == "round_rect":
            rad = int(region.corner_radius)
            if hasattr(draw, "rounded_rectangle"):
                draw.rounded_rectangle(
                    [px, py, px + pw - 1, py + ph - 1],
                    radius=rad,
                    fill=255,
                )
            else:
                self._draw_rounded_rect_fallback(draw, px, py, pw, ph, rad)
        return mask

    @staticmethod
    def _draw_rounded_rect_fallback(draw: ImageDraw.Draw,
                                     x: int, y: int, w: int, h: int,
                                     radius: int):
        """Fallback: manually draw rounded rectangle using arcs + rectangles."""
        r = min(radius, w // 2, h // 2)
        if r <= 0:
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=255)
            return
        # center rectangle
        draw.rectangle([x + r, y, x + w - r - 1, y + h - 1], fill=255)
        draw.rectangle([x, y + r, x + w - 1, y + h - r - 1], fill=255)
        # four corner arcs
        draw.pieslice([x, y, x + r * 2, y + r * 2], 180, 270, fill=255)
        draw.pieslice([x + w - r * 2, y, x + w, y + r * 2], 270, 360, fill=255)
        draw.pieslice([x, y + h - r * 2, x + r * 2, y + h], 90, 180, fill=255)
        draw.pieslice([x + w - r * 2, y + h - r * 2, x + w, y + h], 0, 90, fill=255)

    # ── export ─────────────────────────────────────────────────────────

    def apply_region(self, region: CropRegion, *, sampling=None) -> Image.Image:
        """Crop region from source image, returning RGBA with shape mask applied."""
        iw, ih = self.image.size
        mask = self.generate_mask(region, (iw, ih))
        px, py, pw, ph = region.to_pixel_rect(iw, ih)
        clamped_box = (
            max(0, px),
            max(0, py),
            min(iw, px + pw),
            min(ih, py + ph),
        )
        cropped = self.image.crop(clamped_box).convert("RGBA")
        mask_cropped = mask.crop(clamped_box)
        r, g, b, a = cropped.split()
        merged = Image.merge("RGBA", (r, g, b, ImageChops.multiply(a, mask_cropped)))
        if sampling is not None and sampling.active:
            import numpy as np
            sampling.apply_mask(np.asarray(mask, dtype=np.float32) / np.float32(255))
            sampling.crop(clamped_box)
        return merged
