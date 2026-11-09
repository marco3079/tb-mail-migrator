"""Gera assets/icon.ico e assets/icon.png (requer Pillow)."""

import os

from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(__file__), "..", "assets")


def draw(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 512
    d.rounded_rectangle([16 * s, 16 * s, 496 * s, 496 * s], radius=110 * s, fill=(15, 76, 129))
    # envelope
    env = [96 * s, 150 * s, 416 * s, 360 * s]
    d.rounded_rectangle(env, radius=24 * s, fill=(255, 255, 255))
    d.line([(env[0] + 10 * s, env[1] + 14 * s), (256 * s, 268 * s), (env[2] - 10 * s, env[1] + 14 * s)],
           fill=(15, 76, 129), width=int(18 * s), joint="curve")
    # seta de migração
    d.ellipse([300 * s, 290 * s, 460 * s, 450 * s], fill=(46, 196, 128), outline=(15, 76, 129), width=int(12 * s))
    d.polygon([(340 * s, 352 * s), (395 * s, 352 * s), (395 * s, 326 * s), (432 * s, 370 * s),
               (395 * s, 414 * s), (395 * s, 388 * s), (340 * s, 388 * s)], fill=(255, 255, 255))
    return img


if __name__ == "__main__":
    os.makedirs(ROOT, exist_ok=True)
    img = draw()
    img.save(os.path.join(ROOT, "icon.png"))
    img.save(os.path.join(ROOT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("assets/icon.ico gerado")
