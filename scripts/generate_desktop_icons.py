#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "apps/desktop/src-tauri/icons"
DEFAULT_SOURCE = DEFAULT_OUTPUT_DIR / "icon-source-safe.png"
PNG_SIZES = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 1024,
}
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _load_pillow():
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - local release helper
        print("Pillow is required to generate desktop icons. Install it with: python3 -m pip install Pillow", file=sys.stderr)
        raise SystemExit(2) from exc
    return Image


def _safe_master(source: Path, *, canvas_size: int, visible_scale: float, source_edge_trim: int):
    Image = _load_pillow()
    from PIL import ImageDraw

    original = Image.open(source).convert("RGBA")
    alpha_bounds = original.getbbox()
    if alpha_bounds:
        original = original.crop(alpha_bounds)
    if source_edge_trim > 0 and original.width > source_edge_trim * 2 and original.height > source_edge_trim * 2:
        original = original.crop(
            (
                source_edge_trim,
                source_edge_trim,
                original.width - source_edge_trim,
                original.height - source_edge_trim,
            )
        )
    visible_size = int(canvas_size * visible_scale)
    icon = original.resize((visible_size, visible_size), Image.Resampling.LANCZOS)
    clean_icon = Image.new("RGBA", (visible_size, visible_size), (248, 249, 252, 255))
    clean_icon.alpha_composite(icon)
    mask = Image.new("L", (visible_size, visible_size), 0)
    draw = ImageDraw.Draw(mask)
    radius = int(visible_size * 0.22)
    draw.rounded_rectangle((0, 0, visible_size - 1, visible_size - 1), radius=radius, fill=255)
    clean_icon.putalpha(mask)
    master = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    offset = ((canvas_size - visible_size) // 2, (canvas_size - visible_size) // 2)
    master.alpha_composite(clean_icon, offset)
    return master


def _processed_master(source: Path, *, canvas_size: int):
    Image = _load_pillow()
    master = Image.open(source).convert("RGBA")
    if master.size != (canvas_size, canvas_size):
        master = master.resize((canvas_size, canvas_size), Image.Resampling.LANCZOS)
    return master


def _write_pngs(master, output_dir: Path) -> None:
    Image = _load_pillow()
    for filename, size in PNG_SIZES.items():
        path = output_dir / filename
        image = master.resize((size, size), Image.Resampling.LANCZOS)
        image.save(path)


def _write_ico(master, output_dir: Path) -> None:
    Image = _load_pillow()
    sizes = [(size, size) for size in ICO_SIZES]
    master.save(output_dir / "icon.ico", sizes=sizes)


def _write_icns(master, output_dir: Path) -> None:
    Image = _load_pillow()
    images = [master.resize((size, size), Image.Resampling.LANCZOS) for size in ICNS_SIZES]
    images[-1].save(output_dir / "icon.icns", append_images=images[:-1])


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate padded cross-platform Veyra desktop icons.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--canvas-size", type=int, default=1024)
    parser.add_argument("--visible-scale", type=float, default=0.86, help="Fraction of the canvas used by the source icon.")
    parser.add_argument("--source-edge-trim", type=int, default=32, help="Pixels trimmed from the source alpha bounds before applying the clean mask.")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.exists():
        raise SystemExit(f"missing source icon: {source}")
    if not 0.5 <= args.visible_scale <= 1.0:
        raise SystemExit("--visible-scale must be between 0.5 and 1.0")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_source = (output_dir / "icon-source-safe.png").resolve()
    if source == processed_source:
        master = _processed_master(source, canvas_size=args.canvas_size)
    else:
        master = _safe_master(
            source,
            canvas_size=args.canvas_size,
            visible_scale=args.visible_scale,
            source_edge_trim=args.source_edge_trim,
        )
        master.save(processed_source)
    _write_pngs(master, output_dir)
    _write_ico(master, output_dir)
    _write_icns(master, output_dir)
    print(f"Generated safe Veyra desktop icons from {source} into {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
