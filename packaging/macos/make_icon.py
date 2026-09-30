#!/usr/bin/env python3
"""Generate Ember's coral terminal mark from shared geometry.

    python3 packaging/macos/make_icon.py --sync
    python3 packaging/macos/make_icon.py /path/to/AppIcon.icns
    python3 packaging/macos/make_icon.py /path/to/ember.ico

--sync updates the Linux SVG, Windows ICO, favicon and in-app mark together.
Needs Pillow; macOS ICNS export also needs iconutil. App runtime needs neither.
Legacy asset filenames remain stable for existing desktop launchers.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw


# Canonical identity: all platform and in-app marks derive from these values.
CORAL = "#dd876d"
CHARCOAL = "#20242b"
TILE = (6, 6, 122, 122)
RADIUS = 28
STROKE = 10
PROMPT = ((33, 43), (54, 64), (33, 85))
CURSOR = ((68, 85), (94, 85))
SUPERSAMPLE = 4
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def svg():
    """Return the vector mark used in the browser and Linux launcher."""
    paths = " ".join("M" + " L".join(f"{x} {y}" for x, y in points)
                     for points in (PROMPT, CURSOR))
    x, y, right, bottom = TILE
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" '
            f'width="128" height="128">\n'
            f'  <rect x="{x}" y="{y}" width="{right-x}" height="{bottom-y}" '
            f'rx="{RADIUS}" fill="{CORAL}"/>\n'
            f'  <path d="{paths}" fill="none" stroke="{CHARCOAL}" '
            f'stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round"/>\n'
            f'</svg>\n')


def draw(size):
    """Render the mark at each requested size with supersampled round strokes."""
    scale = size * SUPERSAMPLE / 128
    img = Image.new("RGBA", (size * SUPERSAMPLE, size * SUPERSAMPLE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([v * scale for v in TILE], radius=RADIUS * scale, fill=CORAL)
    width = max(1, round(STROKE * scale))
    for path in (PROMPT, CURSOR):
        points = [(x * scale, y * scale) for x, y in path]
        d.line(points, fill=CHARCOAL, width=width, joint="curve")
        for x, y in points:
            r = width / 2
            d.ellipse((x-r, y-r, x+r, y+r), fill=CHARCOAL)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(out):
    """Write independently rendered Windows taskbar and Explorer sizes."""
    imgs = [draw(s) for s in SIZES]
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES],
                  append_images=imgs[:-1])


def sync_assets():
    """Regenerate shipped assets and replace only the two embedded mark slots."""
    repo = Path(__file__).resolve().parents[2]
    source = svg()
    url = "data:image/svg+xml," + quote(source.strip(), safe="")
    html_path = repo / "index.html"
    html = html_path.read_text(encoding="utf-8")
    html, icons = re.subn(r'<link rel="icon" type="image/svg\+xml" href="[^"]*">',
                         f'<link rel="icon" type="image/svg+xml" href="{url}">', html)
    html, marks = re.subn(r'--ember-icon: url\("[^"]*"\);',
                         f'--ember-icon: url("{url}");', html)
    if (icons, marks) != (1, 1):
        raise ValueError("Expected exactly one favicon and one --ember-icon slot")
    (repo / "launchers/linux/claude-devtools.svg").write_text(source, encoding="utf-8")
    write_ico(repo / "launchers/windows/claude-devtools.ico")
    html_path.write_text(html, encoding="utf-8")
    print("Updated Ember SVG, ICO, favicon and in-app mark.")


def main():
    """Synchronize shipped assets or export a platform icon."""
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    if sys.argv[1] == "--sync":
        sync_assets()
        return 0
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".ico":
        write_ico(out)
    elif out.suffix.lower() == ".icns":
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "AppIcon.iconset"
            iconset.mkdir()
            for s in (16, 32, 64, 128, 256, 512):
                draw(s).save(iconset / f"icon_{s}x{s}.png")
                draw(s * 2).save(iconset / f"icon_{s}x{s}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                           check=True)
    else:
        raise ValueError("Output must be .ico or .icns")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
