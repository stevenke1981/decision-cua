"""Optional set-of-marks screenshot annotation (requires Pillow).

Draws each element's box and id on the screenshot so a *vision* planner can
say "click e12" — the same ids the text outline uses.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from .backends.base import Element, Rect


def annotate(png: bytes, origin: Rect, elements: list[Element], max_side: int = 1600) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    for el in elements:
        if el.rect is None or el.rect.area <= 4:
            continue
        x0, y0 = el.rect.left - origin.left, el.rect.top - origin.top
        x1, y1 = el.rect.right - origin.left, el.rect.bottom - origin.top
        draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 80), width=2)
        tw = 8 * len(el.id) + 6
        draw.rectangle([x0, max(0, y0 - 16), x0 + tw, max(16, y0)], fill=(255, 0, 80))
        draw.text((x0 + 3, max(0, y0 - 16)), el.id, fill=(255, 255, 255), font=font)
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
