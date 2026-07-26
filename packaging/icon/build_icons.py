"""Builds the app's .ico (Windows) and .icns (macOS) icon files from the
single source PNG (app_icon_256.png, the finalized "capitol + arrow"
design). Upscales via Lanczos resampling for the larger sizes both formats
need -- the source art is simple flat shapes with clean edges, so this
holds up fine even though it's not a native high-res render.

Run this whenever the source icon changes; both output files are checked
into the repo (small, and avoids every contributor needing this script's
dependencies just to build the app).
"""
import icnsutil
from PIL import Image

SRC = "final/app_icon_256.png"
ICO_OUT = "final/app_icon.ico"
ICNS_OUT = "final/app_icon.icns"

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
# Explicit ICNS type codes (Apple's format, see icnsutil.IcnsType) rather
# than relying on filename-based guessing -- ic05 (32px "32x32 icon") and
# ic11 (32px "16x16@2x retina") are both 32-pixel PNGs but distinct chunks
# in the file, so an explicit key avoids any ambiguity between them.
ICNS_SIZES = {
    "ic04": 16,    # 16x16
    "ic05": 32,    # 32x32
    "ic07": 128,   # 128x128
    "ic08": 256,   # 256x256
    "ic09": 512,   # 512x512
    "ic10": 1024,  # 512x512@2x / 1024x1024
    "ic11": 32,    # 16x16@2x
    "ic12": 64,    # 32x32@2x
    "ic13": 256,   # 128x128@2x
    "ic14": 512,   # 256x256@2x
}


def resized(img, size):
    return img.resize((size, size), Image.LANCZOS)


def main():
    src = Image.open(SRC).convert("RGBA")

    # .ico -- Pillow writes a proper multi-resolution icon directly.
    src.save(ICO_OUT, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {ICO_OUT} ({ICO_SIZES})")

    # .icns -- build each required size as a temp PNG and let icnsutil pack
    # them into the real Apple icon-family format, keyed by explicit type
    # code so e.g. ic05 (32x32) and ic11 (16x16@2x, also 32px) don't collide.
    img = icnsutil.IcnsFile()
    tmp_paths = []
    for icns_key, px in ICNS_SIZES.items():
        tmp_path = f"final/_tmp_{icns_key}.png"
        resized(src, px).save(tmp_path)
        tmp_paths.append(tmp_path)
        img.add_media(key=icns_key, file=tmp_path)
    img.write(ICNS_OUT)
    print(f"wrote {ICNS_OUT} ({list(ICNS_SIZES.keys())})")

    import os
    for p in tmp_paths:
        os.remove(p)


if __name__ == "__main__":
    main()
