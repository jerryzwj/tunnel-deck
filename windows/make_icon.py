"""生成 TunnelDeck 的 .ico（供 exe 与托盘共用）。"""

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tile(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = max(2, size // 8)
    d.rounded_rectangle([1, 1, size - 2, size - 2], radius=r,
                        fill=(18, 26, 34, 255), outline=(58, 168, 255, 255),
                        width=max(1, size // 32))
    # 简化的 "TD" 字形：两根竖条 + 斜杠 + 横条
    x0, x1 = int(size * 0.20), int(size * 0.31)
    d.rectangle([x0, int(size * 0.30), x1, int(size * 0.70)], fill=(58, 168, 255, 255))
    d.rectangle([int(size * 0.39), int(size * 0.30), int(size * 0.50), int(size * 0.70)],
                fill=(120, 200, 255, 255))
    d.polygon([(int(size * 0.62), int(size * 0.30)), (int(size * 0.80), int(size * 0.30)),
               (int(size * 0.71), int(size * 0.52))], fill=(58, 168, 255, 255))
    d.rectangle([int(size * 0.62), int(size * 0.57), int(size * 0.80), int(size * 0.70)],
                fill=(58, 168, 255, 255))
    return img


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [tile(s) for s in sizes]
    out = os.path.join(ROOT, "windows", "icon.ico")
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes])
    print("icon written:", out, os.path.getsize(out))


if __name__ == "__main__":
    main()
