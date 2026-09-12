"""生成 img2nc.ico —— 紫底 + 奶油色轮廓环(呼应工具主题:图片轮廓→轨迹)。"""
import os

from PIL import Image, ImageDraw

S = 1024                      # 超采样后缩小,边缘更平滑
BG = (42, 18, 48)            # 深紫
BG2 = (28, 22, 38)
CREAM = (233, 220, 201)
ACCENT = (197, 139, 224)     # 淡紫(轨迹光标色)


def rounded_bg(d):
    r = int(S * 0.18)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=BG)
    # 顶部微高光
    d.rounded_rectangle([0, 0, S - 1, int(S * 0.5)], radius=r, fill=BG)


def ring(d):
    cx, cy = S / 2, S / 2
    ow, oh = S * 0.34, S * 0.40   # 外椭圆半径
    iw, ih = S * 0.17, S * 0.22   # 内椭圆半径
    lw = int(S * 0.055)
    d.ellipse([cx - ow, cy - oh, cx + ow, cy + oh], outline=CREAM, width=lw)
    d.ellipse([cx - iw, cy - ih, cx + iw, cy + ih], outline=CREAM, width=lw)
    # 几条横向"栅格/走刀"短线,点出 img2nc 主题
    for fy in (0.40, 0.50, 0.60):
        y = cy - oh + oh * 2 * fy
        d.line([cx - iw * 0.1, y, cx + ow * 0.72, y], fill=CREAM, width=int(S * 0.028))
    # 轨迹光标节点
    nx, ny = cx + ow * 0.71, cy - oh * 0.55
    rr = int(S * 0.05)
    d.ellipse([nx - rr, ny - rr, nx + rr, ny + rr], fill=ACCENT)


def main():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rounded_bg(d)
    ring(d)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img2nc.ico")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(out, format="ICO", sizes=sizes)
    # 附带一张 PNG 预览
    img.resize((256, 256), Image.LANCZOS).save(out.replace(".ico", "_preview.png"))
    print("wrote", out)


if __name__ == "__main__":
    main()
