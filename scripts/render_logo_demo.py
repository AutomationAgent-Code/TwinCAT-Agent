"""Render the TwinCAT Agent logo reveal demo as a PNG frame sequence."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


WIDTH, HEIGHT = 1920, 1080
FPS = 30
DURATION = 6.0
FRAMES = int(FPS * DURATION)
RED = (214, 59, 50)
BG = (242, 245, 248)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def ease(value: float) -> float:
    value = clamp(value)
    return 1 - (1 - value) ** 3


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path("C:/Windows/Fonts/bahnschrift.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def alpha_scaled(image: Image.Image, amount: float) -> Image.Image:
    result = image.copy()
    alpha = result.getchannel("A").point(lambda p: round(p * clamp(amount)))
    result.putalpha(alpha)
    return result


def component_layers(logo: Image.Image) -> dict[str, Image.Image]:
    """Split the existing raster master into T, claws, frame, and spark layers."""
    width, height = logo.size
    alpha = logo.getchannel("A")
    red_mask = Image.new("L", logo.size, 0)
    white_mask = Image.new("L", logo.size, 0)
    pixels = logo.load()
    red_pixels = red_mask.load()
    white_pixels = white_mask.load()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a < 12:
                continue
            if r > 130 and r > g * 1.45 and r > b * 1.45:
                red_pixels[x, y] = a
            elif r + g + b > 500:
                white_pixels[x, y] = a

    claw_region = Image.new("L", logo.size, 0)
    claw_region_draw = ImageDraw.Draw(claw_region)
    claw_region_draw.rectangle(
        (round(width * 0.25), round(height * 0.42), round(width * 0.75), round(height * 0.72)),
        fill=255,
    )
    # Preserve the lower node circles while excluding the nearby outer-frame corners.
    claw_region_draw.rectangle(
        (round(width * 0.27), round(height * 0.70), round(width * 0.37), round(height * 0.75)),
        fill=255,
    )
    claw_region_draw.rectangle(
        (round(width * 0.63), round(height * 0.70), round(width * 0.73), round(height * 0.75)),
        fill=255,
    )
    t_region = Image.new("L", logo.size, 0)
    t_region_draw = ImageDraw.Draw(t_region)
    t_region_draw.rectangle((0, round(height * 0.27), width, round(height * 0.43)), fill=255)
    t_region_draw.rectangle(
        (round(width * 0.42), round(height * 0.40), round(width * 0.58), height),
        fill=255,
    )
    t_white_mask = ImageChops.multiply(white_mask, t_region)
    t_red_region = Image.new("L", logo.size, 0)
    t_red_region_draw = ImageDraw.Draw(t_red_region)
    t_red_region_draw.rectangle(
        (round(width * 0.25), round(height * 0.28), round(width * 0.75), round(height * 0.43)),
        fill=255,
    )
    t_red_region_draw.rectangle(
        (round(width * 0.42), round(height * 0.40), round(width * 0.59), round(height * 0.77)),
        fill=255,
    )
    t_red_region_draw.polygon(
        (
            (round(width * 0.42), round(height * 0.75)),
            (round(width * 0.59), round(height * 0.75)),
            (round(width * 0.505), round(height * 0.84)),
        ),
        fill=255,
    )
    t_red_mask = ImageChops.multiply(red_mask, t_red_region)
    t_mask = ImageChops.lighter(t_white_mask, t_red_mask)
    spark_region = Image.new("L", logo.size, 0)
    ImageDraw.Draw(spark_region).rectangle(
        (round(width * 0.38), 0, round(width * 0.62), round(height * 0.27)),
        fill=255,
    )
    spark_mask = ImageChops.multiply(white_mask, spark_region)
    node_mask = ImageChops.subtract(ImageChops.subtract(white_mask, t_white_mask), spark_mask)

    claws_mask = ImageChops.multiply(red_mask, claw_region)
    claws_mask = ImageChops.subtract(claws_mask, t_red_mask)
    claws_mask = ImageChops.lighter(claws_mask, node_mask)
    frame_mask = ImageChops.subtract(red_mask, ImageChops.multiply(red_mask, claw_region))
    frame_mask = ImageChops.subtract(frame_mask, t_red_mask)

    layers: dict[str, Image.Image] = {}
    for name, mask in (("t", t_mask), ("claws", claws_mask), ("frame", frame_mask), ("spark", spark_mask)):
        layer = logo.copy()
        layer.putalpha(mask)
        layers[name] = layer
    return layers


def centered_scaled(image: Image.Image, scale: float, y: int) -> tuple[Image.Image, tuple[int, int]]:
    size = max(1, round(image.width * scale))
    resized = image.resize((size, size), Image.Resampling.LANCZOS)
    return resized, ((WIDTH - size) // 2, y - size // 2)


def flip_compress(
    image: Image.Image, position: tuple[int, int], horizontal_factor: float
) -> tuple[Image.Image, tuple[int, int]]:
    """Simulate a vertical-axis 3D flip by compressing the mark horizontally."""
    new_width = max(2, round(image.width * horizontal_factor))
    transformed = image.resize((new_width, image.height), Image.Resampling.LANCZOS)
    return transformed, ((WIDTH - new_width) // 2, position[1])


def render(source: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logo = Image.open(source).convert("RGBA")
    logo = ImageEnhance.Color(logo).enhance(0.88)
    layers = component_layers(logo)

    title_font = font(78, bold=True)
    subtitle_font = font(26)

    for frame_number in range(FRAMES):
        t = frame_number / FPS
        canvas = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(canvas, "RGBA")

        # Quiet engineering grid.
        grid_alpha = round(30 * ease(t / 0.8))
        offset = round((t * 18) % 64)
        for x in range(-64 + offset, WIDTH + 64, 64):
            draw.line((x, 0, x, HEIGHT), fill=(104, 117, 132, grid_alpha), width=1)
        for y_grid in range(-64 + offset, HEIGHT + 64, 64):
            draw.line((0, y_grid, WIDTH, y_grid), fill=(104, 117, 132, grid_alpha), width=1)

        # Blueprint-style automation context: rails, I/O nodes, and framing marks.
        context_alpha = round(92 * ease(t / 1.2))
        draw.line((110, 185, 430, 185, 500, 255), fill=(*RED, context_alpha), width=2)
        draw.line((1490, 255, 1560, 185, 1810, 185), fill=(*RED, context_alpha), width=2)
        draw.line((110, 895, 385, 895, 455, 825), fill=(*RED, context_alpha), width=2)
        draw.line((1465, 825, 1535, 895, 1810, 895), fill=(*RED, context_alpha), width=2)
        for x_node, y_node in ((110, 185), (500, 255), (1810, 185), (1490, 255), (110, 895), (455, 825), (1810, 895), (1465, 825)):
            draw.ellipse((x_node - 5, y_node - 5, x_node + 5, y_node + 5), outline=(214, 59, 50, context_alpha + 18), width=2)
        for x_tick in range(160, WIDTH - 120, 160):
            draw.line((x_tick, 118, x_tick, 132), fill=(135, 145, 158, context_alpha), width=1)
            draw.line((x_tick, HEIGHT - 132, x_tick, HEIGHT - 118), fill=(135, 145, 158, context_alpha), width=1)
        draw.rectangle((92, 92, WIDTH - 92, HEIGHT - 92), outline=(118, 130, 145, round(context_alpha * 0.45)), width=1)

        module_alpha = round(58 * ease((t - 0.5) / 1.0))
        for box_x, label in ((170, "I/O"), (1610, "PLC")):
            draw.rounded_rectangle((box_x, 455, box_x + 140, 625), radius=10, outline=(150, 160, 174, module_alpha), width=2)
            draw.text((box_x + 18, 575), label, font=font(18, bold=True), fill=(150, 160, 174, module_alpha + 16))
            for row in range(4):
                cy = 480 + row * 24
                draw.ellipse((box_x + 22, cy, box_x + 30, cy + 8), fill=(*RED, module_alpha + 14))
                draw.line((box_x + 40, cy + 4, box_x + 112, cy + 4), fill=(150, 160, 174, module_alpha), width=1)

        # Moving scan line primes the central T.
        scan_progress = ease((t - 0.1) / 1.0)
        scan_x = round(-180 + (WIDTH + 360) * scan_progress)
        if t < 1.35:
            scan_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            scan_draw = ImageDraw.Draw(scan_layer)
            scan_draw.rectangle((scan_x - 80, 0, scan_x + 80, HEIGHT), fill=(*RED, 18))
            scan_draw.line((scan_x, 0, scan_x, HEIGHT), fill=(*RED, 120), width=3)
            scan_layer = scan_layer.filter(ImageFilter.GaussianBlur(14))
            canvas = Image.alpha_composite(canvas.convert("RGBA"), scan_layer).convert("RGB")

        # 1) The T is the protagonist and locks into place first.
        flip_phase = clamp((t - 2.15) / 0.95)
        flip_factor = 1.0
        if 0 < flip_phase < 1:
            flip_factor = 0.12 + 0.88 * abs(math.cos(math.pi * flip_phase))
        lockup_shift = round(-285 * ease((t - 4.05) / 0.65))

        t_progress = ease((t - 0.30) / 0.85)
        logo_scale = 0.49
        t_layer, logo_pos = centered_scaled(layers["t"], logo_scale * (0.92 + 0.08 * t_progress), 438)
        t_layer = alpha_scaled(t_layer, t_progress)
        t_position = ((WIDTH - t_layer.width) // 2, 438 - t_layer.height // 2)
        t_layer, t_position = flip_compress(t_layer, t_position, flip_factor)
        t_position = (t_position[0] + lockup_shift, t_position[1])
        composed = canvas.convert("RGBA")
        composed.alpha_composite(t_layer, t_position)

        # 2) Circuit "claws" extend symmetrically from the T toward their nodes.
        claw_progress = ease((t - 1.00) / 1.25)
        claw_layer, claw_pos = centered_scaled(layers["claws"], logo_scale, 438)
        claw_reveal = Image.new("L", claw_layer.size, 0)
        claw_draw = ImageDraw.Draw(claw_reveal)
        half_width = round(claw_layer.width * 0.50 * claw_progress)
        center_x = claw_layer.width // 2
        claw_draw.rectangle((center_x - half_width, 0, center_x + half_width, claw_layer.height), fill=255)
        claw_reveal = claw_reveal.filter(ImageFilter.GaussianBlur(3))
        claw_layer.putalpha(ImageChops.multiply(claw_layer.getchannel("A"), claw_reveal))
        claw_layer, claw_pos = flip_compress(claw_layer, claw_pos, flip_factor)
        claw_pos = (claw_pos[0] + lockup_shift, claw_pos[1])
        composed.alpha_composite(claw_layer, claw_pos)

        # 3) The outer controller frame traces downward along both sides, meeting at the tip.
        frame_progress = ease((t - 2.05) / 1.75)
        frame_layer, frame_pos = centered_scaled(layers["frame"], logo_scale, 438)
        frame_reveal = Image.new("L", frame_layer.size, 0)
        frame_draw = ImageDraw.Draw(frame_reveal)
        reveal_y = round(frame_layer.height * frame_progress)
        frame_draw.rectangle((0, 0, frame_layer.width, reveal_y), fill=255)
        frame_reveal = frame_reveal.filter(ImageFilter.GaussianBlur(5))
        frame_layer.putalpha(ImageChops.multiply(frame_layer.getchannel("A"), frame_reveal))
        frame_layer, frame_pos = flip_compress(frame_layer, frame_pos, flip_factor)
        frame_pos = (frame_pos[0] + lockup_shift, frame_pos[1])
        composed.alpha_composite(frame_layer, frame_pos)

        # Soft red aura follows the growing geometry.
        aura_strength = clamp((t - 0.7) / 0.7) * (1 - 0.55 * clamp((t - 4.2) / 1.0))
        aura = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        aura_source = alpha_scaled(Image.alpha_composite(claw_layer, frame_layer), 0.30 * aura_strength)
        aura_source = aura_source.filter(ImageFilter.GaussianBlur(26))
        aura.alpha_composite(aura_source, frame_pos)
        composed = Image.alpha_composite(aura, composed)

        # 4) Spark is a final AI activation confirmation.
        spark_progress = ease((t - 3.75) / 0.42)
        spark_layer, spark_pos = centered_scaled(layers["spark"], logo_scale, 438)
        spark_layer = alpha_scaled(spark_layer, spark_progress)
        spark_position = ((WIDTH - spark_layer.width) // 2, 438 - spark_layer.height // 2)
        spark_layer, spark_position = flip_compress(spark_layer, spark_position, flip_factor)
        spark_position = (spark_position[0] + lockup_shift, spark_position[1])
        composed.alpha_composite(spark_layer, spark_position)

        # Signal nodes pulse in three deterministic beats.
        if 1.45 < t < 2.75:
            pulse_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            pulse_draw = ImageDraw.Draw(pulse_layer)
            for index, y_node in enumerate((410, 478, 548)):
                phase = clamp((t - (1.45 + index * 0.22)) / 0.42)
                radius = 8 + round(22 * phase)
                alpha = round(130 * math.sin(math.pi * phase))
                for x_node in (785, 1135):
                    x_node = round(WIDTH / 2 + (x_node - WIDTH / 2) * flip_factor) + lockup_shift
                    pulse_draw.ellipse(
                        (x_node - radius, y_node - radius, x_node + radius, y_node + radius),
                        outline=(*RED, alpha),
                        width=4,
                    )
            pulse_layer = pulse_layer.filter(ImageFilter.GaussianBlur(3))
            composed = Image.alpha_composite(composed, pulse_layer)

        # AI activation flash over the existing star.
        flash_phase = clamp((t - 3.78) / 0.58)
        if 0 < flash_phase < 1:
            flash_alpha = round(185 * math.sin(math.pi * flash_phase))
            flash_radius = round(26 + 70 * flash_phase)
            flash = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            flash_draw = ImageDraw.Draw(flash)
            flash_draw.ellipse(
                (960 - flash_radius, 205 - flash_radius, 960 + flash_radius, 205 + flash_radius),
                fill=(255, 255, 255, flash_alpha),
            )
            flash = flash.filter(ImageFilter.GaussianBlur(28))
            composed = Image.alpha_composite(composed, flash)

        # Brand lockup.
        text_progress = ease((t - 4.15) / 0.62)
        if text_progress > 0:
            text_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            text_draw = ImageDraw.Draw(text_layer)
            title = "TwinCAT Agent"
            title_x = 1060
            title_y = round(420 + 18 * (1 - text_progress))
            text_draw.text((title_x, title_y), title, font=title_font, fill=(31, 37, 45, round(255 * text_progress)))

            subtitle = "AI AUTOMATION DEVELOPMENT"
            subtitle_x = title_x + 4
            text_draw.text(
                (subtitle_x, title_y + 112),
                subtitle,
                font=subtitle_font,
                fill=(*RED, round(230 * text_progress)),
            )
            text_draw.line(
                (title_x - 38, title_y - 8, title_x - 38, title_y + 152),
                fill=(*RED, round(190 * text_progress)),
                width=3,
            )
            composed = Image.alpha_composite(composed, text_layer)

        # Cinematic edge vignette.
        vignette = Image.new("L", (WIDTH, HEIGHT), 0)
        vignette_draw = ImageDraw.Draw(vignette)
        vignette_draw.ellipse((-220, -300, WIDTH + 220, HEIGHT + 310), fill=215)
        vignette = vignette.filter(ImageFilter.GaussianBlur(170))
        darkness = Image.new("RGBA", (WIDTH, HEIGHT), (25, 31, 40, 12))
        darkness.putalpha(Image.eval(vignette, lambda p: 255 - p))
        composed = Image.alpha_composite(composed, darkness)

        composed.convert("RGB").save(output_dir / f"frame-{frame_number:04d}.png", quality=94)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: render_logo_demo.py SOURCE_LOGO OUTPUT_DIR")
    render(Path(sys.argv[1]), Path(sys.argv[2]))
