#!/usr/bin/env python3
"""Render Android launcher icons (mipmap-*/ic_launcher.png) from a cover PNG.

    python tools/android_icon.py <cover.png> <res_dir>
"""
import os
import sys

from PIL import Image, ImageDraw

SIZES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def main():
    src, res = sys.argv[1], sys.argv[2]
    im = Image.open(src).convert("RGBA")
    # square-crop to the centre, then round the corners a little
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    for name, px in SIZES.items():
        out = im.resize((px, px), Image.LANCZOS)
        mask = Image.new("L", (px, px), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, px - 1, px - 1), radius=px // 5, fill=255)
        out.putalpha(mask)
        d = os.path.join(res, "mipmap-" + name)
        os.makedirs(d, exist_ok=True)
        out.save(os.path.join(d, "ic_launcher.png"))
    print("icons ->", res)


if __name__ == "__main__":
    main()
