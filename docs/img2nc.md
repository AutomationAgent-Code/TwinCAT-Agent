# img2nc — 图片切片成 TwinCAT NCI G 代码

把灰度/彩色图片栅格扫描成 TwinCAT NCI 解释器可直接执行的 **DIN 66025** G 代码,
用 **Z 轴抬落笔/刀** 渲染。纯 Pillow + numpy,不依赖 TwinCAT / COM。

## 安装

```bash
pip install -e ".[img2nc]"     # 装 pillow + numpy,并注册 tc-img2nc 命令
```

## 图形界面 (Web UI)

```bash
py -m tc_template.img2nc_ui          # 启动后自动开浏览器 http://127.0.0.1:8799
py -m tc_template.img2nc_ui --port 8888 --no-browser
```

左侧面板可调**全部参数**并分组：模式 · **轴行程 X/Y/Z** · 密度/分辨率 · Z 轴 · 进给 · 输出；
拖拽/点击上传图片 → 「生成预览」→ 右侧**实时轨迹**(落笔/空程分色) + 统计(尺寸·落笔·空程·估时)
→ 「下载 .nc」。切换 contour/binary/depth 时相关参数自动显隐。

> contour 模式依赖 cv2,须用 `py`(Python 3.14)启动 UI。

## 两种切片模式

| 模式 | 说明 | 适用 |
|------|------|------|
| `binary`(默认) | 灰度 Floyd–Steinberg 抖动成点阵,逐行(弓字形)扫描;暗处 **G1 落笔(Z 下)**,亮处 **G0 抬笔(Z 上)**。同行连续暗像素合并成一段 G1。 | 照片/线稿,写字·绘图·激光·刻线机器人 |
| `depth` | 灰度线性映射到 **Z 深度**,连续走刀做浮雕。暗=深,亮=浅,纯白留白抬刀。 | 灰度浮雕/V 型雕刻 |
| `contour` | 阈值分割后用 OpenCV 提取所有**闭合轮廓**(含内外圈/孔洞/字母),Douglas-Peucker 简化,每条轮廓一次落笔描边。**只走线不填充**,行数/耗时最省。 | Logo/线稿/文字描边,写字机器人 |

> **contour 模式需要 `opencv-python`(首选)或 `scikit-image`(自动回退)**。本机 cv2 装在 Python 3.14 下,
> 用 `py -m tc_template.img2nc_cli ...` 运行(而非 `python`)。

## 常用命令

```bash
# 照片 → 点阵,工件宽 120mm,精度 0.4mm,落笔 -0.5mm
tc-img2nc photo.jpg -w 120 --step 0.4 --z-down -0.5

# Logo → 灰度浮雕,最深 -2mm,同时导出雕刻预览供核对
tc-img2nc logo.png -m depth --z-down -2 --preview logo_prev.png

# Logo → 轮廓描边(只走线),浅色 Logo 深底需 --invert;需用 py(cv2 在 3.14)
py -m tc_template.img2nc_cli logo.png -w 120 -m contour --invert --preview c.png

# 白线黑底图需反色;指定输出路径
tc-img2nc sign.png --invert -o C:\NcFiles\sign.nc
```

## 关键参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `-w/--width` | 100 | 工件宽度 mm(高度按图片长宽比自动,或 `--height` 指定) |
| `--step` | 0.5 | 扫描点 X 间距 mm。**越小越精细、文件越大** |
| `--line-step` | 同 step | 行间距 mm |
| `-m/--mode` | binary | `binary` / `depth` / `contour` |
| `--contour-res` | 0.25 | contour 提取分辨率 mm(越小越平滑) |
| `--simplify` | 0.15 | contour Douglas-Peucker 简化容差 mm(0=不简化) |
| `--blur` | 0.8 | contour 提取前高斯平滑 sigma 像素(0=关) |
| `--min-contour` | 1.5 | 丢弃周长小于此值的轮廓 mm(去毛刺) |
| `--dither` | floyd | `floyd`(误差扩散,细腻)/ `ordered`(Bayer)/ `none`(硬阈值 `--threshold`) |
| `--invert` | 关 | 反色(默认"暗=雕刻") |
| `--flip-x` | 关 | 水平镜像(机器 X 轴方向相反时) |
| `--flip-y` | 关 | 垂直镜像(机器 Y 轴方向相反时) |
| `--gamma` | 1.0 | 灰度伽马,>1 变暗 |
| `--z-safe` | 5 | 空程安全抬刀高度 mm |
| `--z-up` | 1 | 段间悬停高度 mm |
| `--z-down` | -1 | binary 落笔深度 / depth 最深(纯黑)深度 mm |
| `--z-top` | 0 | depth 最浅(纯白)Z / 工件表面 mm |
| `--feed-cut` | 1000 | XY 切削进给 mm/min |
| `--feed-plunge` | 300 | Z 下扎进给 mm/min |
| `--travel-mode` | rapid | 空程方式:`rapid`=G0 快速 / `feed`=G1 指定速度 |
| `--feed-travel` | 2000 | 空程进给 mm/min(`--travel-mode feed` 时生效) |
| `--origin` | bottom-left | 原点(Y 向上);`top-left` = Y 向下 |
| `--no-serpentine` | — | 关闭弓字形(始终同向扫描) |
| `--line-numbers` | — | 输出 N 行号 |
| `--preview PATH` | — | 导出将被雕刻的点阵/灰度预览 PNG |

## 输出格式(TwinCAT NCI 直接可读)

- **ASCII 编码 + CRLF 换行**,程序头 `G17 G90 G71`(XY 平面 / 绝对 / 公制 mm)。
- 注释用 DIN 66025 圆括号 `(...)`,程序以 `M30` 结束。
- 坐标原点默认工件左下角,图片顶行映射到最大 Y。
- 空程/抬刀用 `G0`(快速),切削/下扎用 `G1 ... F`(带进给)。

生成后在 TwinCAT 里用 `ItpLoadProgram` / NCI 通道加载该 `.nc` 文件即可运行。
> 落笔深度、进给、安全高度请按实际刀具/笔和材料调整,首次务必空跑校验行程边界。

## Python API

```python
from tc_template.img2nc import NciConfig, ImageToNci, convert_image_to_nci

cfg = NciConfig(width_mm=120, pixel_step_mm=0.4, mode="binary", z_down=-0.5)
code = convert_image_to_nci("photo.jpg", "photo.nc", cfg)   # 返回 G 代码字符串并写盘

conv = ImageToNci(cfg)
conv.preview_image("photo.jpg").save("prev.png")            # 雕刻预览
```
