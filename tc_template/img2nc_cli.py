"""tc-img2nc — 命令行:图片切片成 TwinCAT NCI G 代码。"""
from __future__ import annotations

import os
import sys

import click

from .img2nc import NciConfig, ImageToNci


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("image", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--out", type=click.Path(dir_okay=False),
              help="输出 .nc 路径 (默认:图片同名 .nc)")
# 尺寸 / 分辨率
@click.option("-w", "--width", "width_mm", default=100.0, show_default=True,
              help="工件宽度 mm (高度按长宽比自动)")
@click.option("--height", "height_mm", type=float, default=None,
              help="工件高度 mm (省略=按长宽比)")
@click.option("--step", "pixel_step_mm", default=0.5, show_default=True,
              help="扫描点 X 间距 mm (越小越细、文件越大)")
@click.option("--line-step", "line_step_mm", type=float, default=None,
              help="行间距 mm (省略=同 --step)")
# 模式
@click.option("-m", "--mode", type=click.Choice(["binary", "depth", "contour"]),
              default="binary", show_default=True,
              help="binary=抬落笔点阵 / depth=灰度浮雕 / contour=轮廓描边")
@click.option("--dither", type=click.Choice(["floyd", "ordered", "none"]),
              default="floyd", show_default=True, help="binary 抖动算法")
@click.option("--threshold", default=128, show_default=True,
              help="二值阈值 0-255 (dither=none 及 contour 模式用)")
# contour 轮廓描边
@click.option("--contour-res", "contour_res_mm", default=0.25, show_default=True,
              help="contour 提取分辨率 mm (越小越平滑)")
@click.option("--simplify", "simplify_mm", default=0.15, show_default=True,
              help="contour 简化容差 mm (0=不简化)")
@click.option("--blur", "blur_px", default=0.8, show_default=True,
              help="contour 提取前平滑 sigma 像素 (0=关)")
@click.option("--min-contour", "min_contour_mm", default=1.5, show_default=True,
              help="丢弃周长小于此值的轮廓 mm")
# 图像预处理
@click.option("--invert", is_flag=True, help="反色 (白线黑底图需要)")
@click.option("--flip-x", is_flag=True, help="水平镜像 (机器 X 轴方向相反时)")
@click.option("--flip-y", is_flag=True, help="垂直镜像 (机器 Y 轴方向相反时)")
@click.option("--gamma", default=1.0, show_default=True, help="灰度伽马 (>1 变暗)")
@click.option("--no-skip-white", is_flag=True,
              help="depth 模式:连纯白也走刀(默认留白抬刀)")
# Z 轴
@click.option("--z-safe", default=5.0, show_default=True, help="安全抬刀高度 mm")
@click.option("--z-up", default=1.0, show_default=True, help="段间悬停高度 mm")
@click.option("--z-down", default=-1.0, show_default=True,
              help="binary 落笔深度 / depth 最深(纯黑)深度 mm")
@click.option("--z-top", default=0.0, show_default=True,
              help="depth 最浅(纯白)Z / 工件表面 mm")
# 进给
@click.option("--feed-cut", default=1000.0, show_default=True, help="XY 切削进给 mm/min")
@click.option("--feed-plunge", default=300.0, show_default=True, help="Z 下扎进给 mm/min")
@click.option("--travel-mode", type=click.Choice(["rapid", "feed"]), default="feed",
              show_default=True, help="空程方式: feed=G1 指定速度(默认) / rapid=G0 快速")
@click.option("--feed-travel", default=2000.0, show_default=True,
              help="空程进给 mm/min (--travel-mode feed 时生效)")
# 输出
@click.option("--origin", type=click.Choice(["bottom-left", "top-left"]),
              default="bottom-left", show_default=True, help="坐标原点")
@click.option("--no-serpentine", is_flag=True, help="关闭弓字形(始终同向扫描)")
@click.option("--line-numbers", is_flag=True, help="输出 N 行号")
@click.option("--preview", type=click.Path(dir_okay=False),
              help="同时导出雕刻预览 PNG 供核对")
def main(image, out, width_mm, height_mm, pixel_step_mm, line_step_mm, mode, dither,
         threshold, contour_res_mm, simplify_mm, blur_px, min_contour_mm,
         invert, flip_x, flip_y, gamma, no_skip_white, z_safe, z_up, z_down, z_top,
         feed_cut, feed_plunge, travel_mode, feed_travel, origin, no_serpentine,
         line_numbers, preview):
    """把 IMAGE 图片切片成 TwinCAT NCI (DIN 66025) G 代码。

    示例:

        tc-img2nc photo.jpg -w 120 --step 0.4 --z-down -0.5

        tc-img2nc logo.png -m depth --z-down -2 --preview logo_prev.png
    """
    cfg = NciConfig(
        width_mm=width_mm, height_mm=height_mm,
        pixel_step_mm=pixel_step_mm, line_step_mm=line_step_mm,
        mode=mode, dither=dither, threshold=threshold,
        contour_res_mm=contour_res_mm, simplify_mm=simplify_mm,
        blur_px=blur_px, min_contour_mm=min_contour_mm,
        invert=invert, flip_x=flip_x, flip_y=flip_y, gamma=gamma, skip_white=not no_skip_white,
        z_safe=z_safe, z_up=z_up, z_down=z_down, z_top=z_top,
        feed_cut=feed_cut, feed_plunge=feed_plunge,
        travel_mode=travel_mode, feed_travel=feed_travel,
        origin=origin, serpentine=not no_serpentine, line_numbers=line_numbers,
        program_name=os.path.splitext(os.path.basename(image))[0].upper()[:20] or "IMG2NCI",
    )

    if out is None:
        out = os.path.splitext(image)[0] + ".nc"

    conv = ImageToNci(cfg)
    try:
        code = conv.convert(image)
    except Exception as e:  # noqa: BLE001
        click.echo(f"[ERROR] 转换失败: {e}", err=True)
        sys.exit(1)

    with open(out, "w", encoding="ascii", newline="\r\n") as f:
        f.write(code)

    n_lines = code.count("\n")
    click.echo(f"[OK] {image} -> {out}")
    click.echo(f"     模式={mode} 抖动={dither} 行数={n_lines}")

    if preview:
        conv.preview_image(image).save(preview)
        click.echo(f"     预览 -> {preview}")


if __name__ == "__main__":
    main()
