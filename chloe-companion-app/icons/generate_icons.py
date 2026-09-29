#!/usr/bin/env python3
"""Generate PWA icons for Chloe — AI Companion from the source photo.

Source is a phone screenshot (1206x2622); the script crops the PHOTO REGION
only (rows 413..2209, no status bar / screenshot bars) and takes a square
centered on her face/upper body, then resizes with LANCZOS.

Splash screens below are kept from the earlier placeholder motif pass.
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.expanduser(
    "~/workspace/user/media_library/image/e8/"
    "e86f8d305ee416b0d0b1cda97ba02e56bdd8096c4894a49d0f7d6378be25fe59.png"
)

# Photo region inside the screenshot (detected by row-brightness scan).
PHOTO_TOP, PHOTO_BOT = 413, 2209
# Square crop centered on her face/upper body (verified visually).
CROP_CX, CROP_CY, CROP_SIDE = 562, 1000 - PHOTO_TOP, 1100


def base_crop():
    im = Image.open(SRC).convert("RGB")
    w, _ = im.size
    half = CROP_SIDE // 2
    x0 = min(max(CROP_CX - half, 0), w - CROP_SIDE)
    # CROP_CY is in photo coordinates; translate back to screenshot rows.
    y0 = PHOTO_TOP + max(CROP_CY - half, 0)
    return im.crop((x0, y0, x0 + CROP_SIDE, y0 + CROP_SIDE))


def make_icon(px, out):
    img = base_crop().resize((px, px), Image.LANCZOS)
    img.save(out, optimize=True)
    print("wrote", out, img.size)


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0], size[1]],
                                       radius=radius, fill=255)
    return m


NAVY_TOP = (11, 19, 48)
NAVY_BOT = (6, 10, 28)
AMBER = (232, 169, 76)
AMBER_SOFT = (244, 217, 160)


def vgrad(size, top, bot):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        col = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = col
    return img


def make_splash(w, h, out):
    import math, random
    img = vgrad((w, h), NAVY_TOP, NAVY_BOT).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(11)
    for _ in range(140):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h * 0.7)
        r = rnd.uniform(0.8, 2.2)
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=(244, 217, 160, int(rnd.uniform(60, 200))))
    # coin ring + crescent motif, centered
    cx, cy, r = w // 2, int(h * 0.42), int(w * 0.20)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [cx - r * 1.35, cy - r * 1.35, cx + r * 1.35, cy + r * 1.35],
        fill=(232, 169, 76, 40))
    img.alpha_composite(glow)
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              outline=AMBER + (255,), width=max(3, int(r * 0.07)))
    cr = r * 0.55
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=AMBER_SOFT + (255,))
    d.ellipse([cx - cr * 0.85, cy - cr * 1.15, cx + cr * 1.15, cy + cr * 0.85],
              fill=NAVY_TOP + (255,))
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            f1 = ImageFont.truetype(p, int(w * 0.075))
            f2 = ImageFont.truetype(p, int(w * 0.032))
            break
    else:
        f1 = f2 = ImageFont.load_default()
    d.text((w // 2, int(h * 0.58)), "Chloe", font=f1,
           fill=AMBER_SOFT + (255,), anchor="mm")
    d.text((w // 2, int(h * 0.63)), "She stays. She remembers.", font=f2,
           fill=(154, 163, 196, 255), anchor="mm")
    img.convert("RGB").save(out)
    print("wrote", out)


if __name__ == "__main__":
    os.makedirs(HERE, exist_ok=True)
    make_icon(512, os.path.join(HERE, "icon-512.png"))
    make_icon(192, os.path.join(HERE, "icon-192.png"))
    # Apple touch icon: 180px, full-bleed (iOS rounds it itself).
    make_icon(180, os.path.join(HERE, "apple-touch-icon.png"))
