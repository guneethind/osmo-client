"""Draws assets/osmo-client.ico (the app and tray icon): a camera lens on a rounded dark square.
Run once after changing the design:  python packaging/make_icon.py"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> Image.Image:
    scale = 8                               # drawn large, then reduced: smooth edges at every size
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=(24, 28, 36, 255))
    c = s / 2
    for radius, color in ((0.34, (70, 80, 98, 255)), (0.26, (14, 16, 22, 255)), (0.18, (43, 123, 255, 255)),
                          (0.09, (14, 16, 22, 255))):
        r = s * radius
        d.ellipse((c - r, c - r, c + r, c + r), fill=color)
    r = s * 0.045                           # highlight
    d.ellipse((c - s * 0.09 - r, c - s * 0.09 - r, c - s * 0.09 + r, c - s * 0.09 + r), fill=(255, 255, 255, 230))
    return image.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "assets" / "osmo-client.ico"
    target.parent.mkdir(exist_ok=True)
    draw(256).save(target, format="ICO", sizes=[(n, n) for n in SIZES], append_images=[draw(n) for n in SIZES])
    print(target)
