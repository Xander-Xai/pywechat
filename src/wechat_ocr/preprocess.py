"""Conservative image views for tiny contact-panel text."""

from __future__ import annotations

from PIL import Image, ImageEnhance, ImageOps


def recognition_views(
    image: Image.Image, scale: int = 2
) -> tuple[Image.Image, Image.Image]:
    """Return source and grayscale/contrast views at a bounded scale.

    No thresholding or morphology is used: thin underscores, hyphens and
    glyph strokes must survive preprocessing.
    """
    rgb = image.convert("RGB")
    if scale not in (1, 2, 3):
        raise ValueError("supported OCR diagnostic scales are 1x, 2x, and 3x")
    size = (max(1, rgb.width * scale), max(1, rgb.height * scale))
    view_a = rgb if scale == 1 else rgb.resize(size, Image.Resampling.LANCZOS)
    view_b = ImageEnhance.Contrast(ImageOps.grayscale(rgb)).enhance(1.35)
    return view_a, view_b if scale == 1 else view_b.resize(
        size, Image.Resampling.LANCZOS
    )
