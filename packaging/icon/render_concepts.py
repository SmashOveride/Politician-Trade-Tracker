"""Renders the app icon's source art directly with Pillow (already a project
dependency) instead of an SVG renderer, since this machine has no native
Cairo library for cairosvg to bind to. Produces the "capitol + uptrend
arrow" design used by packaging/icon/build_icons.py to build the actual
.ico/.icns files.

Run this whenever the design itself needs to change, then re-run
build_icons.py to regenerate final/app_icon.ico and final/app_icon.icns."""
from PIL import Image, ImageDraw

SIZE = 256


def rounded_bg(color):
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=48, fill=color)
    return img, d


def concept1():
    img, d = rounded_bg("#132339")

    # Capitol building: 30% bigger than the original, then another 10% on
    # top of that (1.3 * 1.1 = 1.43 total) -- still anchored at the base's
    # own center/bottom so it keeps growing in place rather than drifting.
    b_scale = 1.3 * 1.1
    b_anchor_x, b_anchor_y = 131, 212

    def sx(x):
        return b_anchor_x + (x - b_anchor_x) * b_scale

    def sy(y):
        return b_anchor_y + (y - b_anchor_y) * b_scale

    def sbox(box):
        x1, y1, x2, y2 = box
        return [sx(x1), sy(y1), sx(x2), sy(y2)]

    d.ellipse(sbox([120, 100, 136, 116]), fill="#e8c766")
    d.pieslice(sbox([92, 82, 164, 154]), 180, 360, fill="#f4f6fa")
    d.rectangle(sbox([100, 118, 156, 136]), fill="#f4f6fa")
    d.rectangle(sbox([82, 136, 174, 150]), fill="#dfe4ee")
    for x in (86, 106, 126, 146, 166):
        d.rectangle(sbox([x, 150, x + 10, 196]), fill="#dfe4ee")
    d.rectangle(sbox([72, 196, 190, 212]), fill="#c7ceda")

    # Arrow: 10% bigger, anchored at its own starting point (bottom-left)
    # so it grows toward the upper-right rather than drifting off-center.
    a_scale = 1.1
    a_anchor_x, a_anchor_y = 46, 208

    def ax(x):
        return a_anchor_x + (x - a_anchor_x) * a_scale

    def ay(y):
        return a_anchor_y + (y - a_anchor_y) * a_scale

    def apts(coords):
        return [ax(x) if i % 2 == 0 else ay(x) for i, x in enumerate(coords)]

    arrow_width = round(12 * a_scale)
    d.line(apts([46, 208, 96, 158, 132, 186, 206, 96]), fill="#2ecc71", width=arrow_width, joint="curve")
    d.line(apts([176, 92, 210, 92, 210, 126]), fill="#2ecc71", width=arrow_width, joint="curve")
    img.save("final/app_icon_256.png")


concept1()
print("rendered final/app_icon_256.png")
