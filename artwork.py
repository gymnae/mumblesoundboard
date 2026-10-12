"""Renders now-playing artwork into still video frames for the Schnackn bots.

Square art (album covers) sits in a Win98-style bevelled box next to the
title; wide art (YouTube thumbnails) fills the frame under a title bar.
"""
import io
import os
from collections import namedtuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

# key: changes whenever the picture or text changes (the bot re-renders then).
# load: returns encoded image bytes or None; only called when key changes.
Artwork = namedtuple('Artwork', 'key title subtitle load')

DEFAULT_COVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'cover_default.png')

NAVY = (0, 0, 128)
GREEN = (0, 255, 0)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
SILVER = (192, 192, 192)
GREY = (128, 128, 128)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Wider than this counts as a video thumbnail rather than a cover.
WIDE_ASPECT = 1.3


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without FreeType: fixed-size bitmap font
        return ImageFont.load_default()


def _open(image_bytes):
    """(image, is_pixel_art). Falls back to the bundled default cover."""
    if image_bytes:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
            return img.convert('RGB'), False
        except Exception:
            pass
    return Image.open(DEFAULT_COVER).convert('RGB'), True


def _wrap(draw, text, font, width, max_lines):
    words = text.split()
    lines = []
    while words and len(lines) < max_lines:
        line = words.pop(0)
        while words and draw.textlength(line + ' ' + words[0], font=font) <= width:
            line += ' ' + words.pop(0)
        lines.append(line)
    if words or (lines and draw.textlength(lines[-1], font=font) > width):
        last = lines[-1]
        while last and draw.textlength(last + '…', font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + '…'
    return lines


def _bevel(draw, box, width=3):
    """Raised Win98 bevel: light top/left, dark bottom/right."""
    x0, y0, x1, y1 = box
    for i in range(width):
        draw.line([(x0 + i, y1 - i), (x0 + i, y0 + i), (x1 - i, y0 + i)], fill=WHITE if i == 0 else SILVER)
        draw.line([(x0 + i, y1 - i), (x1 - i, y1 - i), (x1 - i, y0 + i)], fill=BLACK if i == 0 else GREY)


def _text_block(draw, x, y, width, label, title, subtitle, scale):
    draw.text((x, y), label, font=_font(int(18 * scale)), fill=RED)
    y += int(30 * scale)
    font = _font(int(26 * scale))
    for line in _wrap(draw, title or '', font, width, 3):
        draw.text((x, y), line, font=font, fill=GREEN)
        y += int(32 * scale)
    if subtitle:
        font = _font(int(20 * scale))
        for line in _wrap(draw, subtitle, font, width, 2):
            y += int(4 * scale)
            draw.text((x, y), line, font=font, fill=YELLOW)
            y += int(22 * scale)


def render_frame(image_bytes, title, subtitle=None, width=640, height=360, label='ON AIR'):
    """Compose one RGBA frame (raw bytes, width*height*4) for a LiveKit VideoFrame."""
    art, pixel_art = _open(image_bytes)
    resample = Image.NEAREST if pixel_art else Image.LANCZOS
    scale = height / 360
    canvas = Image.new('RGB', (width, height), NAVY)
    draw = ImageDraw.Draw(canvas)

    if art.width / art.height >= WIDE_ASPECT:
        canvas.paste(ImageOps.fit(art, (width, height), resample), (0, 0))
        bar = int(64 * scale)
        draw.rectangle([0, height - bar, width, height], fill=NAVY)
        draw.line([(0, height - bar), (width, height - bar)], fill=GREEN, width=max(2, int(3 * scale)))
        pad = int(14 * scale)
        draw.text((pad, height - bar + pad), label, font=_font(int(16 * scale)), fill=RED)
        text_x = pad + int(90 * scale)
        font = _font(int(22 * scale))
        line = _wrap(draw, title or '', font, width - text_x - pad, 1)
        draw.text((text_x, height - bar + int(10 * scale)), line[0] if line else '', font=font, fill=GREEN)
        sub = _wrap(draw, subtitle or '', _font(int(16 * scale)), width - text_x - pad, 1)
        if sub:
            draw.text((text_x, height - bar + int(36 * scale)), sub[0], font=_font(int(16 * scale)), fill=YELLOW)
    else:
        margin = int(20 * scale)
        side = height - 2 * margin
        _bevel(draw, (margin - 4, margin - 4, margin + side + 3, margin + side + 3), width=4)
        canvas.paste(ImageOps.fit(art, (side, side), resample), (margin, margin))
        text_x = margin * 2 + side
        _text_block(draw, text_x, margin, width - text_x - margin, label, title, subtitle, scale)

    draw.rectangle([0, 0, width - 1, height - 1], outline=GREEN, width=max(1, int(2 * scale)))
    return canvas.convert('RGBA').tobytes()
