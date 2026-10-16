"""Draw the integration's brand icon: a generic bathroom scale, no trademarks.

Run `uv run python tools/brand_icon.py` to regenerate
custom_components/beurer_scales/brand/icon.png (256 px) and icon@2x.png (512 px).
"""

from pathlib import Path

from PIL import Image, ImageDraw

BRAND = Path(__file__).parent.parent / "custom_components" / "beurer_scales" / "brand"
CANVAS = 2048  # drawn large, then scaled down for smooth edges

PLATFORM = (3, 155, 229, 255)  # blue
DISPLAY = (255, 255, 255, 255)
DETAIL = (1, 87, 155, 255)  # dark blue


def draw() -> Image.Image:
    """The icon at CANVAS size on a transparent background."""
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    s = CANVAS / 256  # design units: a 256 grid

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[int, ...]:
        return tuple(round(v * s) for v in (x0, y0, x1, y1))

    # Platform of the scale.
    d.rounded_rectangle(box(16, 16, 240, 240), radius=round(44 * s), fill=PLATFORM)
    # Display window with a needle and tick marks.
    d.rounded_rectangle(box(64, 44, 192, 108), radius=round(18 * s), fill=DISPLAY)
    for x in (88, 108, 148, 168):
        d.line(box(x, 58, x, 68), fill=DETAIL, width=round(5 * s))
    d.line(box(128, 96, 140, 58), fill=DETAIL, width=round(7 * s))
    d.ellipse(box(120, 88, 136, 104), fill=DETAIL)
    # Two foot pads.
    d.rounded_rectangle(box(56, 136, 116, 212), radius=round(26 * s), fill=DETAIL)
    d.rounded_rectangle(box(140, 136, 200, 212), radius=round(26 * s), fill=DETAIL)
    return image


def main() -> None:
    BRAND.mkdir(exist_ok=True)
    image = draw()
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        image.resize((size, size), Image.Resampling.LANCZOS).save(
            BRAND / name, optimize=True
        )


if __name__ == "__main__":
    main()
