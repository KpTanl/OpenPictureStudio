from PIL import Image, ImageChops, ImageDraw, ImageFilter


class LassoTool:
    """Build polygon masks and apply lasso cutouts."""

    @staticmethod
    def _normalize_points(points: list[tuple[int, int]], size: tuple[int, int]) -> list[tuple[int, int]]:
        width, height = size
        normalized: list[tuple[int, int]] = []
        last_point = None

        for x, y in points:
            cx = max(0, min(width - 1, int(round(x))))
            cy = max(0, min(height - 1, int(round(y))))
            current = (cx, cy)
            if current != last_point:
                normalized.append(current)
                last_point = current

        return normalized

    def generate_mask(self, size: tuple[int, int], points: list[tuple[int, int]]) -> Image.Image:
        """Generate an 8-bit mask from polygon points (inside=255)."""
        mask = Image.new("L", size, 0)
        normalized = self._normalize_points(points, size)
        if len(normalized) < 3:
            return mask

        draw = ImageDraw.Draw(mask)
        draw.polygon(normalized, fill=255)
        return mask

    def build_selection_mask(self, size: tuple[int, int], regions: list[dict]) -> Image.Image:
        """Compose a mask from multiple regions.

        Region item format:
            {"points": [(x, y), ...], "op": "add" | "subtract"}
        """
        selection = Image.new("L", size, 0)
        if not regions:
            return selection

        for region in regions:
            points = region.get("points", []) if isinstance(region, dict) else []
            op = region.get("op", "add") if isinstance(region, dict) else "add"
            region_mask = self.generate_mask(size, points)
            if op == "subtract":
                selection = ImageChops.subtract(selection, region_mask)
            else:
                selection = ImageChops.lighter(selection, region_mask)

        return selection

    def cutout_by_regions(
        self,
        image: Image.Image,
        regions: list[dict],
        keep_inside: bool = True,
        feather_radius: int = 0,
        *,
        sampling=None,
    ) -> Image.Image:
        """Apply multi-region lasso to alpha and return RGBA output."""
        rgba = image.convert("RGBA")
        mask = self.build_selection_mask(rgba.size, regions)

        if not keep_inside:
            mask = ImageChops.invert(mask)

        if feather_radius > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(radius=feather_radius))

        r, g, b, alpha = rgba.split()
        merged_alpha = ImageChops.multiply(alpha, mask)
        if sampling is not None and sampling.active:
            import numpy as np
            sampling.apply_mask(np.asarray(mask, dtype=np.float32) / np.float32(255))
        return Image.merge("RGBA", (r, g, b, merged_alpha))

    def cutout(
        self,
        image: Image.Image,
        points: list[tuple[int, int]],
        invert: bool = False,
        feather_radius: int = 0,
    ) -> Image.Image:
        """Backward compatible single-region API."""
        return self.cutout_by_regions(
            image,
            [{"points": points, "op": "add"}],
            keep_inside=not invert,
            feather_radius=feather_radius,
        )
