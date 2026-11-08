"""Render the PNG app icons at image build time (Pillow), matching static/icon.svg."""
from PIL import Image, ImageDraw

GREEN, LID, JAR, BAND = "#2f6b4f", "#e9c46a", "#f6f4ef", "#e3efe8"


def draw(size, maskable=False):
    s = size / 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle([0, 0, size, size], fill=GREEN)
        inset, k = size * 0.1, 0.8           # keep the jar inside the maskable safe zone
    else:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=112 * s, fill=GREEN)
        inset, k = 0, 1.0
    t = lambda x, y: (inset + x * s * k, inset + y * s * k)
    box = lambda x0, y0, x1, y1: [*t(x0, y0), *t(x1, y1)]
    d.rounded_rectangle(box(176, 96, 336, 140), radius=14 * s * k, fill=LID)
    d.rounded_rectangle(box(146, 132, 366, 424), radius=56 * s * k, fill=JAR)
    d.rectangle(box(146, 222, 366, 334), fill=BAND)
    d.ellipse(box(198, 252, 314, 322), fill=GREEN)
    d.line([t(256, 252), t(284, 210)], fill=GREEN, width=max(2, int(10 * s * k)))
    return img


if __name__ == "__main__":
    import sys
    out = sys.argv[1]
    draw(192).save(f"{out}/icon-192.png")
    draw(512).save(f"{out}/icon-512.png")
    draw(512, maskable=True).save(f"{out}/icon-maskable-512.png")
