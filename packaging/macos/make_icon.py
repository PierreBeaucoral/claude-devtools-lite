#!/usr/bin/env python3
"""Render the Claude DevTools app icon into an .icns file.

    python3 make_icon.py /path/to/AppIcon.icns

Optional helper for the macOS build — needs Pillow and `iconutil` (macOS).
The app works fine without an icon.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m, r = size * 0.06, size * 0.22
    d.rounded_rectangle([m, m, size - m, size - m], radius=r,
                        fill=(13, 17, 23, 255), outline=(45, 51, 59, 255),
                        width=max(1, size // 128))
    lw = max(2, int(size * 0.075))
    x0, y0 = size * 0.24, size * 0.34                      # prompt chevron
    d.line([(x0, y0), (x0 + size * 0.14, size * 0.5), (x0, size * 0.66)],
           fill=(88, 166, 255, 255), width=lw, joint="curve")
    d.rounded_rectangle([size * 0.47, size * 0.60,         # cursor
                         size * 0.68, size * 0.60 + lw],
                        radius=lw / 2, fill=(210, 153, 34, 255))
    cx, cy, s = size * 0.68, size * 0.34, size * 0.10      # spark
    d.polygon([(cx, cy - s), (cx + s * .35, cy - s * .1), (cx + s, cy),
               (cx + s * .35, cy + s * .1), (cx, cy + s),
               (cx - s * .35, cy + s * .1), (cx - s, cy),
               (cx - s * .35, cy - s * .1)], fill=(63, 185, 80, 255))
    return img


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for s in (16, 32, 64, 128, 256, 512, 1024):
            draw(s).save(iconset / f"icon_{s}x{s}.png")
            if s <= 512:
                draw(s * 2).save(iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                       check=True)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
