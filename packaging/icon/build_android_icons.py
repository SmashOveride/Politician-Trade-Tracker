"""Builds Android launcher icon assets (legacy mipmap PNGs + adaptive-icon
foreground/background) from the same single source PNG the other platforms
use (app_icon_256.png -- see build_icons.py). Outputs land directly under
the Android project's res/ tree.

Run this whenever the source icon changes; outputs are checked into the
repo like the .ico/.icns files are, so contributors building the Android
app don't need this script's dependencies either.
"""
import os

from PIL import Image

SRC = "final/app_icon_256.png"
RES_DIR = os.path.join("..", "..", "android", "app", "src", "main", "res")

# Legacy (pre-API 26) launcher icon sizes -- the source art already bakes in
# its own rounded-square background, so these are a straight resize, same
# idea as the .ico/.icns sizes in build_icons.py.
LEGACY_SIZES = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}

# Adaptive icon (API 26+) foreground layer canvas sizes (108dp scaled per
# density bucket). The OS applies its own mask/shape to this layer, so
# content has to stay inside a centered "safe zone" or it gets clipped by
# some launchers -- scaling the source down before centering it on a
# transparent canvas achieves that.
ADAPTIVE_SIZES = {
    "mipmap-mdpi": 108,
    "mipmap-hdpi": 162,
    "mipmap-xhdpi": 216,
    "mipmap-xxhdpi": 324,
    "mipmap-xxxhdpi": 432,
}
SAFE_ZONE_SCALE = 0.62

# A bit further in from the true corner than it looks like it needs to be,
# inside the source's flat navy fill -- the rounded-square background has a
# generous corner radius, so points as far in as (10, 10) are still in the
# fully transparent corner cutout. (20, 20) is confirmed clear of that.
BACKGROUND_SAMPLE_PX = (20, 20)


def resized(img, size):
    return img.resize((size, size), Image.LANCZOS)


def main():
    src = Image.open(SRC).convert("RGBA")
    bg_color = src.getpixel(BACKGROUND_SAMPLE_PX)

    for bucket, size in LEGACY_SIZES.items():
        out_dir = os.path.join(RES_DIR, bucket)
        os.makedirs(out_dir, exist_ok=True)
        icon = resized(src, size)
        icon.save(os.path.join(out_dir, "ic_launcher.png"))
        icon.save(os.path.join(out_dir, "ic_launcher_round.png"))
    print(f"wrote legacy launcher icons to {list(LEGACY_SIZES)}")

    for bucket, canvas_size in ADAPTIVE_SIZES.items():
        out_dir = os.path.join(RES_DIR, bucket)
        os.makedirs(out_dir, exist_ok=True)
        art_size = round(canvas_size * SAFE_ZONE_SCALE)
        art = resized(src, art_size)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        offset = ((canvas_size - art_size) // 2, (canvas_size - art_size) // 2)
        canvas.paste(art, offset, art)
        canvas.save(os.path.join(out_dir, "ic_launcher_foreground.png"))
    print(f"wrote adaptive icon foreground layers to {list(ADAPTIVE_SIZES)}")

    values_dir = os.path.join(RES_DIR, "values")
    os.makedirs(values_dir, exist_ok=True)
    hex_color = "#%02X%02X%02X" % bg_color[:3]
    colors_path = os.path.join(values_dir, "ic_launcher_background.xml")
    with open(colors_path, "w", encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<resources>\n"
            f'    <color name="ic_launcher_background">{hex_color}</color>\n'
            "</resources>\n"
        )
    print(f"wrote {colors_path} (sampled background {hex_color})")

    anydpi_dir = os.path.join(RES_DIR, "mipmap-anydpi-v26")
    os.makedirs(anydpi_dir, exist_ok=True)
    adaptive_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <background android:drawable="@color/ic_launcher_background" />\n'
        '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n'
        "</adaptive-icon>\n"
    )
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        with open(os.path.join(anydpi_dir, name), "w", encoding="utf-8") as f:
            f.write(adaptive_xml)
    print(f"wrote adaptive icon XML to {anydpi_dir}")


if __name__ == "__main__":
    main()
