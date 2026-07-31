#!/usr/bin/env python
"""Generate the packaged app's icon and Windows version resource.

Both are build products, so they are generated here instead of committed. The
icon is original artwork drawn with the same shapes the tray already renders;
no vendor logo is used or redistributed.

Usage: python scripts/build_assets.py --version 0.1.0 --out build
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
DISC = (20, 21, 29, 255)
TRACK = (148, 163, 184, 105)
ARC = (34, 197, 94, 255)
MARK = (232, 232, 240, 255)


def draw_icon(size: int) -> Image.Image:
    # Drawn large and downsampled: PIL's arc has no antialiasing of its own.
    scale = 8
    edge = size * scale
    pad = edge * 5 // 64
    width = max(1, edge * 10 // 64)
    img = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box = (pad, pad, edge - pad, edge - pad)
    inset = pad + width // 2
    draw.ellipse((inset, inset, edge - inset, edge - inset), fill=DISC)
    draw.arc(box, 0, 360, fill=TRACK, width=width)
    draw.arc(box, -90, 150, fill=ARC, width=width)

    font = ImageFont.load_default(size=int(edge * 0.39))
    left, top, right, bottom = draw.textbbox((0, 0), "F", font=font)
    draw.text(
        ((edge - right + left) / 2, (edge - bottom + top) / 2 - top),
        "F",
        fill=MARK,
        font=font,
    )
    return img.resize((size, size), Image.LANCZOS)


def write_icon(path: Path) -> None:
    frames = [draw_icon(size) for size in ICON_SIZES]
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES])


def _quad(version: str) -> tuple[int, int, int, int]:
    parts = []
    for chunk in version.lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    parts = (parts + [0, 0, 0, 0])[:4]
    return tuple(parts)  # type: ignore[return-value]


def write_version_resource(path: Path, version: str) -> None:
    """Write a PyInstaller --version-file so Windows shows real file details."""
    quad = _quad(version)
    display = version.lstrip("vV") or "0.0.0"
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Fleet contributors'),
          StringStruct('FileDescription', 'Fleet - agent usage tray'),
          StringStruct('FileVersion', '{display}'),
          StringStruct('InternalName', 'Fleet'),
          StringStruct('LegalCopyright', 'MIT License'),
          StringStruct('OriginalFilename', 'Fleet.exe'),
          StringStruct('ProductName', 'Fleet'),
          StringStruct('ProductVersion', '{display}'),
        ],
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="dev")
    parser.add_argument("--out", default="build", type=Path)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    write_icon(args.out / "fleet.ico")
    write_version_resource(args.out / "version-info.txt", args.version)


if __name__ == "__main__":
    main()
