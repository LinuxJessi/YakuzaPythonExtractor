"""make_icon.py — draw the app icon (a red 龍 on black) and write icon.ico + icon.icns.

    python3 packaging/make_icon.py [--font /path/to/a/CJK.ttf]

Needs Pillow. The committed icons were drawn with Hiragino Sans GB on macOS;
any font with the 龍 glyph will do (Noto Sans CJK, Source Han Sans …).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:/Windows/Fonts/msgothic.ttc", "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


def draw(size: int, font_path: str) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size * 0.22
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=(14, 10, 12, 255))
    # a thin gold rim, inset
    inset = max(1, size // 40)
    d.rounded_rectangle((inset, inset, size - 1 - inset, size - 1 - inset), radius=r - inset,
                        outline=(233, 196, 106, 255), width=max(1, size // 64))
    font = ImageFont.truetype(font_path, int(size * 0.72))
    glyph = "龍"
    bbox = d.textbbox((0, 0), glyph, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1]
    # glow: the glyph blurred on its own layer, then the sharp glyph on top
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x, y), glyph, font=font, fill=(232, 52, 58, 255))
    if size >= 64:
        glow = layer.filter(ImageFilter.GaussianBlur(size / 28))
        r, g, b, a = glow.split()
        glow = Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * 0.8))))
        img.alpha_composite(glow)
    img.alpha_composite(layer)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font")
    a = ap.parse_args()
    font = a.font or next((c for c in CANDIDATES if os.path.exists(c)), None)
    if not font:
        sys.exit("no CJK font found; pass --font")
    base = draw(1024, font)
    base.save(HERE / "icon.png")
    base.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    base.save(HERE / "icon.icns")
    print(f"wrote icon.png / icon.ico / icon.icns with {font}")


if __name__ == "__main__":
    main()
