"""img2nc — 图片切片成 TwinCAT NCI 可执行的 G 代码 (DIN 66025)。

栅格扫描模式:把灰度图逐行(弓字形)扫描,用 Z 轴抬落笔/刀渲染:
  - binary 模式(默认): Floyd–Steinberg 抖动成点阵,暗处笔落走 G1、亮处抬笔走 G0。
  - depth  模式:        灰度线性映射到 Z 深度,做连续浮雕(relief)雕刻。

输出为 TwinCAT NCI 解释器直接可读的 DIN 66025 G 代码:
  G17 (XY 平面) / G90 (绝对) / G71 (公制 mm) / F 进给 mm/min / M30 结束 / (...) 注释。

用法见 CLI (`tc-img2nc`) 或 `ImageToNci` 类。纯 Pillow + numpy,无 COM、无 TwinCAT 依赖。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class NciConfig:
    """图片 → NCI G 代码的全部可调参数。"""

    # --- 工件尺寸 / 分辨率 ---
    width_mm: float = 100.0            # 输出工件宽度 (mm)。高度按图片长宽比自动算,除非指定 height_mm。
    height_mm: Optional[float] = None  # 输出工件高度 (mm);None = 按长宽比
    pixel_step_mm: float = 0.5         # 每个扫描像素/点在 X 方向的物理间距 (mm) —— 越小越精细、文件越大
    line_step_mm: Optional[float] = None  # 行间距 (mm);None = 与 pixel_step_mm 相同(正方点阵)

    # --- 图像预处理 ---
    invert: bool = False               # 反色:默认"暗=雕刻"。白底黑线的图通常不用反;白线黑底需 invert
    flip_x: bool = False               # 水平镜像(左右翻转);机器 X 轴方向相反时用
    flip_y: bool = False               # 垂直镜像(上下翻转);机器 Y 轴方向相反时用
    gamma: float = 1.0                 # 灰度伽马校正 (>1 变暗, <1 变亮)
    threshold: int = 128               # binary 模式无抖动时的二值阈值 (0-255)
    dither: str = "floyd"              # binary 抖动: "floyd" | "ordered" | "none"

    # --- 切片模式 ---
    mode: str = "binary"               # "binary" (抬落笔) | "depth" (浮雕) | "contour" (轮廓描边)
    skip_white: bool = True            # depth 模式:纯白像素抬笔不雕(留白)

    # --- contour 轮廓描边参数 ---
    contour_res_mm: float = 0.25       # 轮廓提取采样分辨率 mm/像素(越小轮廓越平滑)
    simplify_mm: float = 0.15          # Douglas-Peucker 简化容差 mm(0=不简化)
    blur_px: float = 0.8               # 提取前高斯平滑 sigma(像素,0=关);抑制锯齿
    min_contour_mm: float = 1.5        # 丢弃周长小于此值的轮廓 mm(去毛刺/噪点)

    # --- Z 轴 (mm) ---
    z_safe: float = 5.0                # 快速移动/换段时的抬笔安全高度 (G0)
    z_up: float = 1.0                  # 笔尖悬停高度(段间小抬,可与 z_safe 相同)
    z_down: float = -1.0               # binary: 落笔/切削深度。depth: 最深(纯黑)深度
    z_top: float = 0.0                 # depth: 最浅(纯白)对应的 Z (工件表面)

    # --- 进给 (mm/min) ---
    feed_cut: float = 1000.0           # XY 切削进给 (G1)
    feed_plunge: float = 300.0         # Z 下扎进给 (G1)
    travel_mode: str = "feed"          # 空程方式: "feed"(G1 指定速度,默认) | "rapid"(G0 快速)
    feed_travel: float = 2000.0        # travel_mode="feed" 时的空程进给 (mm/min)

    # --- 输出选项 ---
    origin: str = "bottom-left"        # 坐标原点: "bottom-left"(Y 向上,默认) | "top-left"(Y 向下)
    flip_x: bool = False               # 水平镜像(左右翻转,修正机器 X 方向相反导致的镜像)
    flip_y: bool = False               # 垂直翻转(上下翻转)
    serpentine: bool = True            # 弓字形扫描(交替行方向)以减少空程
    line_numbers: bool = False         # 输出 N 行号
    decimals: int = 3                  # 坐标小数位
    program_name: str = "IMG2NCI"      # 头部注释里的程序名


# --------------------------------------------------------------------------- #
# 图像处理
# --------------------------------------------------------------------------- #
def _load_gray(path_or_img, cfg: NciConfig, res_mm: Optional[float] = None) -> np.ndarray:
    """读图 → 灰度 → 缩放到目标点阵 → 预处理。返回 uint8 数组, shape (rows, cols), 0=黑 255=白。

    res_mm 指定采样分辨率(正方像素);None 时用 pixel_step_mm / line_step_mm。
    """
    if isinstance(path_or_img, Image.Image):
        img = path_or_img
    else:
        img = Image.open(path_or_img)

    # 透明通道贴白底,避免透明区变黑
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = ImageOps.exif_transpose(img).convert("L")

    # 目标点阵尺寸(列 = 宽度方向, 行 = 高度方向)
    x_step = res_mm or cfg.pixel_step_mm
    line_step = res_mm or cfg.line_step_mm or cfg.pixel_step_mm
    cols = max(1, round(cfg.width_mm / x_step))
    if cfg.height_mm is not None:
        rows = max(1, round(cfg.height_mm / line_step))
    else:
        aspect = img.height / img.width
        rows = max(1, round(cols * x_step * aspect / line_step))

    img = img.resize((cols, rows), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float64)

    if cfg.gamma and cfg.gamma != 1.0:
        arr = 255.0 * np.power(np.clip(arr / 255.0, 0, 1), cfg.gamma)
    if cfg.invert:
        arr = 255.0 - arr
    if cfg.flip_x:                     # 水平镜像:左右翻转 -> 输出左右镜像
        arr = np.fliplr(arr)
    if cfg.flip_y:                     # 垂直镜像:上下翻转 -> 输出上下镜像
        arr = np.flipud(arr)

    return np.clip(arr, 0, 255).astype(np.uint8)


def _floyd_steinberg(gray: np.ndarray) -> np.ndarray:
    """Floyd–Steinberg 误差扩散抖动。返回 bool 数组: True = 落笔(暗)。"""
    a = gray.astype(np.float64).copy()
    h, w = a.shape
    for y in range(h):
        for x in range(w):
            old = a[y, x]
            new = 0.0 if old < 128 else 255.0
            err = old - new
            a[y, x] = new
            if x + 1 < w:
                a[y, x + 1] += err * 7 / 16
            if y + 1 < h:
                if x > 0:
                    a[y + 1, x - 1] += err * 3 / 16
                a[y + 1, x] += err * 5 / 16
                if x + 1 < w:
                    a[y + 1, x + 1] += err * 1 / 16
    return a < 128


_BAYER8 = np.array([
    [0, 48, 12, 60, 3, 51, 15, 63], [32, 16, 44, 28, 35, 19, 47, 31],
    [8, 56, 4, 52, 11, 59, 7, 55], [40, 24, 36, 20, 43, 27, 39, 23],
    [2, 50, 14, 62, 1, 49, 13, 61], [34, 18, 46, 30, 33, 17, 45, 29],
    [10, 58, 6, 54, 9, 57, 5, 53], [42, 26, 38, 22, 41, 25, 37, 21],
], dtype=np.float64) / 64.0 * 255.0


def _ordered(gray: np.ndarray) -> np.ndarray:
    """有序抖动 (8x8 Bayer)。返回 bool: True = 落笔。"""
    h, w = gray.shape
    tile = np.tile(_BAYER8, (h // 8 + 1, w // 8 + 1))[:h, :w]
    return gray.astype(np.float64) < tile


def to_engrave_mask(gray: np.ndarray, cfg: NciConfig) -> np.ndarray:
    """返回布尔点阵: True = 需要落笔雕刻的像素。"""
    if cfg.dither == "floyd":
        return _floyd_steinberg(gray)
    if cfg.dither == "ordered":
        return _ordered(gray)
    return gray < cfg.threshold  # none: 硬阈值


# --------------------------------------------------------------------------- #
# G 代码生成
# --------------------------------------------------------------------------- #
class _Emitter:
    def __init__(self, cfg: NciConfig):
        self.cfg = cfg
        self.lines: List[str] = []
        self._n = 10

    def _num(self, v: float) -> str:
        s = f"{v:.{self.cfg.decimals}f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"

    def raw(self, text: str):
        if self.cfg.line_numbers:
            self.lines.append(f"N{self._n} {text}")
            self._n += 10
        else:
            self.lines.append(text)

    def comment(self, text: str):
        self.lines.append(f"({text})")

    def move(self, g: int, x=None, y=None, z=None, f=None):
        parts = [f"G{g}"]
        if x is not None:
            parts.append(f"X{self._num(x)}")
        if y is not None:
            parts.append(f"Y{self._num(y)}")
        if z is not None:
            parts.append(f"Z{self._num(z)}")
        if f is not None:
            parts.append(f"F{self._num(f)}")
        self.raw(" ".join(parts))


def _travel(em: _Emitter, cfg: NciConfig, x=None, y=None, z=None):
    """XY 空程移动:默认 G0 快速;travel_mode='feed' 时用 G1 指定进给(可控速度)。"""
    if cfg.travel_mode == "feed":
        em.move(1, x=x, y=y, z=z, f=cfg.feed_travel)
    else:
        em.move(0, x=x, y=y, z=z)


def _px_to_mm(col: int, row: int, rows: int, cols: int, cfg: NciConfig) -> Tuple[float, float]:
    """点阵坐标(col,row) → 工件坐标 mm。row 0 = 图片顶部。含镜像/翻转。"""
    line_step = cfg.line_step_mm or cfg.pixel_step_mm
    x = col * cfg.pixel_step_mm
    if cfg.flip_x:
        x = (cols - 1) * cfg.pixel_step_mm - x
    # 行方向: bottom-left 时图片顶行映射到最大 Y
    yrow = row if cfg.origin == "top-left" else (rows - 1 - row)
    if cfg.flip_y:
        yrow = (rows - 1) - yrow
    return x, yrow * line_step


def _emit_header(em: _Emitter, cfg: NciConfig, rows: int, cols: int, extra: str = ""):
    step = cfg.contour_res_mm if cfg.mode == "contour" else cfg.pixel_step_mm
    line_step = step if cfg.mode == "contour" else (cfg.line_step_mm or cfg.pixel_step_mm)
    w = cols * step
    h = rows * line_step
    em.comment(f"{cfg.program_name} - image to TwinCAT NCI G-code")
    tag = f"mode={cfg.mode}" + (f" {extra}" if extra else f" dither={cfg.dither}")
    em.comment(f"{tag} grid={cols}x{rows}")
    em.comment(f"work area {w:.2f} x {h:.2f} mm  res={step} mm")
    em.raw("G17 G90 G71")               # XY 平面, 绝对, 公制 mm
    em.move(0, z=cfg.z_safe)            # 抬到安全高度


def _emit_binary(em: _Emitter, mask: np.ndarray, cfg: NciConfig):
    """binary 抬落笔: 每行把连续暗像素合并成一段 G1,段间抬笔。"""
    rows, cols = mask.shape
    for r in range(rows):
        row = mask[r]
        segs: List[Tuple[int, int]] = []  # (col_start, col_end) 含端点
        c = 0
        while c < cols:
            if row[c]:
                s = c
                while c < cols and row[c]:
                    c += 1
                segs.append((s, c - 1))
            else:
                c += 1
        if not segs:
            continue
        # 弓字形:奇数行反向,减少空程
        if cfg.serpentine and (r % 2 == 1):
            segs = [(e, s) for (s, e) in reversed(segs)]
        for (s, e) in segs:
            x0, y0 = _px_to_mm(min(s, e), r, rows, cfg)
            x1, y1 = _px_to_mm(max(s, e), r, rows, cfg)
            if s > e:  # 反向段
                x0, x1 = x1, x0
            _travel(em, cfg, x=x0, y=y0, z=cfg.z_up)     # 空程抬笔到段首
            em.move(1, z=cfg.z_down, f=cfg.feed_plunge)  # 落笔
            em.move(1, x=x1, f=cfg.feed_cut)             # 沿行切削到段尾
            em.move(0, z=cfg.z_up)                        # 抬笔


def _emit_depth(em: _Emitter, gray: np.ndarray, cfg: NciConfig):
    """depth 浮雕: 灰度线性映射到 Z, 弓字形连续走刀。暗=深, 亮=浅。"""
    rows, cols = gray.shape
    g = gray.astype(np.float64) / 255.0        # 0=黑 1=白
    depth = cfg.z_top + (cfg.z_down - cfg.z_top) * (1.0 - g)  # 白→z_top, 黑→z_down
    engaged = False
    for r in range(rows):
        order = range(cols)
        if cfg.serpentine and (r % 2 == 1):
            order = range(cols - 1, -1, -1)
        first = True
        for c in order:
            x, y = _px_to_mm(c, r, rows, cfg)
            white = gray[r, c] >= 250
            if cfg.skip_white and white:
                if engaged:
                    em.move(0, z=cfg.z_safe)   # 遇留白抬刀
                    engaged = False
                continue
            z = depth[r, c]
            if not engaged:
                _travel(em, cfg, x=x, y=y, z=cfg.z_safe)   # 定位
                em.move(1, z=z, f=cfg.feed_plunge)         # 下扎
                engaged = True
            else:
                if first:
                    em.move(1, x=x, y=y, z=z, f=cfg.feed_cut)
                else:
                    em.move(1, x=x, z=z, f=cfg.feed_cut)
            first = False
    if engaged:
        em.move(0, z=cfg.z_safe)


# --------------------------------------------------------------------------- #
# contour 轮廓描边
# --------------------------------------------------------------------------- #
def extract_contours(gray: np.ndarray, cfg: NciConfig) -> List[np.ndarray]:
    """从灰度点阵提取闭合轮廓。返回像素坐标数组列表, 每条 shape (N,2) = (col,row)。

    优先用 OpenCV findContours(含内外圈/孔洞) + approxPolyDP 简化;
    无 cv2 时退回 skimage.measure marching-squares。
    """
    res = cfg.contour_res_mm
    eps_px = max(cfg.simplify_mm / res, 0.0)
    min_perim_px = cfg.min_contour_mm / res
    fg = (gray < cfg.threshold).astype(np.uint8) * 255  # 雕刻区 = 前景

    try:
        import cv2
        if cfg.blur_px and cfg.blur_px > 0:
            g = cv2.GaussianBlur(gray, (0, 0), cfg.blur_px)
            fg = (g < cfg.threshold).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(fg, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in cnts:
            if cv2.arcLength(c, True) < min_perim_px:
                continue
            if eps_px > 0:
                c = cv2.approxPolyDP(c, eps_px, True)
            pts = c.reshape(-1, 2).astype(np.float64)
            if len(pts) >= 2:
                out.append(pts)
        return out
    except ImportError:
        from skimage import measure
        from skimage.filters import gaussian
        g = gray.astype(np.float64)
        if cfg.blur_px and cfg.blur_px > 0:
            g = gaussian(g, sigma=cfg.blur_px) * 255.0
        out = []
        for ct in measure.find_contours(g, cfg.threshold):
            poly = measure.approximate_polygon(ct, tolerance=eps_px) if eps_px > 0 else ct
            perim = np.sum(np.hypot(*np.diff(poly, axis=0).T))
            if perim < min_perim_px or len(poly) < 2:
                continue
            out.append(poly[:, ::-1])  # (row,col) -> (col,row)
        return out


def _emit_contour(em: _Emitter, contours: List[np.ndarray], rows: int, cfg: NciConfig):
    """每条闭合轮廓:抬笔空移到起点 → 落笔 → G1 沿轮廓 → 回到起点闭合 → 抬笔。"""
    res = cfg.contour_res_mm

    def to_mm(col, row):
        x = col * res
        y = row * res if cfg.origin == "top-left" else (rows - 1 - row) * res
        return x, y

    for pts in contours:
        x0, y0 = to_mm(pts[0, 0], pts[0, 1])
        _travel(em, cfg, x=x0, y=y0, z=cfg.z_up)       # 空移到起点
        em.move(1, z=cfg.z_down, f=cfg.feed_plunge)    # 落笔
        for p in pts[1:]:
            x, y = to_mm(p[0], p[1])
            em.move(1, x=x, y=y, f=cfg.feed_cut)       # 沿轮廓
        em.move(1, x=x0, y=y0, f=cfg.feed_cut)         # 闭合回起点
        em.move(0, z=cfg.z_up)                          # 抬笔


def _emit_footer(em: _Emitter, cfg: NciConfig):
    em.move(0, z=cfg.z_safe)         # 抬到安全高度(快速抬刀)
    _travel(em, cfg, x=0, y=0)       # 回原点(遵循空程方式,默认 G1 受控)
    em.raw("M30")                    # 程序结束


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
class ImageToNci:
    def __init__(self, cfg: Optional[NciConfig] = None):
        self.cfg = cfg or NciConfig()

    def convert(self, path_or_img) -> str:
        cfg = self.cfg
        em = _Emitter(cfg)
        if cfg.mode == "contour":
            gray = _load_gray(path_or_img, cfg, res_mm=cfg.contour_res_mm)
            rows, cols = gray.shape
            contours = extract_contours(gray, cfg)
            _emit_header(em, cfg, rows, cols, extra=f"contours={len(contours)}")
            _emit_contour(em, contours, rows, cfg)
        else:
            gray = _load_gray(path_or_img, cfg)
            rows, cols = gray.shape
            _emit_header(em, cfg, rows, cols)
            if cfg.mode == "depth":
                _emit_depth(em, gray, cfg)
            else:
                mask = to_engrave_mask(gray, cfg)
                _emit_binary(em, mask, cfg)
        _emit_footer(em, cfg)
        return "\n".join(em.lines) + "\n"

    def preview_image(self, path_or_img) -> Image.Image:
        """返回将被雕刻的预览:binary=黑白点阵, depth=缩放灰度, contour=轮廓线。"""
        cfg = self.cfg
        if cfg.mode == "contour":
            from PIL import ImageDraw
            gray = _load_gray(path_or_img, cfg, res_mm=cfg.contour_res_mm)
            rows, cols = gray.shape
            contours = extract_contours(gray, cfg)
            im = Image.new("L", (cols, rows), 255)
            d = ImageDraw.Draw(im)
            for pts in contours:
                seq = [(float(p[0]), float(p[1])) for p in pts]
                d.line(seq + [seq[0]], fill=0, width=1)
            return im
        gray = _load_gray(path_or_img, cfg)
        if cfg.mode == "depth":
            return Image.fromarray(gray, "L")
        mask = to_engrave_mask(gray, cfg)
        return Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), "L")


def convert_image_to_nci(path_or_img, out_path: Optional[str] = None,
                         cfg: Optional[NciConfig] = None) -> str:
    """便捷函数:转换并可选写盘。返回 G 代码字符串。"""
    code = ImageToNci(cfg).convert(path_or_img)
    if out_path:
        with io.open(out_path, "w", encoding="ascii", newline="\r\n") as f:
            f.write(code)
    return code
