"""Decoding, cropping and converting uploaded cover images.

Accepts everything Pillow reads (JPEG, PNG, GIF, WebP, AVIF, TIFF, BMP, ICO,
TGA, JPEG 2000, PSD, QOI, PPM, ...) plus HEIC/HEIF photos via pillow-heif.
SVG is rasterized by the browser before it is sent. Every image is turned
upright (EXIF orientation), converted to sRGB (Display P3 phone photos, CMYK
print files) and stored as JPEG, or PNG when it has transparency.

Covers are square (COVER_ASPECT): the admin page lets the user choose the
square, and sends it as fractions of the upright image, so the same numbers
work for the original file and for any downscaled preview of it.
"""
import io
import math

from PIL import Image, ImageCms, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except ImportError:  # optional: without it HEIC/HEIF uploads are refused
    HEIF_SUPPORT = False

COVER_ASPECT = 1.0          # width / height of a cover
COVER_MAX_PX = 1000         # longest edge stored in the sound file
PREVIEW_MAX_PX = 1600       # longest edge of the cropping preview
MIN_CROP_PX = 16
MAX_INPUT_BYTES = 25 * 1024 * 1024
# Refuse decompression bombs early (Pillow only warns below 2x its default).
Image.MAX_IMAGE_PIXELS = 80_000_000

FORMAT_NAMES = ('JPEG', 'PNG', 'GIF', 'WebP', 'AVIF', 'TIFF', 'BMP', 'ICO', 'TGA', 'JPEG 2000',
                'PSD', 'QOI', 'PPM') + (('HEIC/HEIF',) if HEIF_SUPPORT else ()) + ('SVG',)
# For the file picker; image/* alone misses HEIC/PSD/TGA on some systems.
ACCEPT = ','.join(['image/*', '.heic', '.heif', '.avif', '.jxl', '.tif', '.tiff', '.psd', '.tga',
                   '.jp2', '.j2k', '.qoi', '.ppm', '.pgm', '.pbm', '.ico', '.bmp', '.svg'])


class ImageError(ValueError):
    """The image cannot be used (unreadable, too large, bad crop)."""


def parse_crop(raw):
    """'x,y,w,h' (fractions of the upright image) -> tuple, None when empty.
    Also accepts a list/tuple or a dict with x, y, w(idth), h(eight)."""
    if raw is None or raw == '':
        return None
    try:
        if isinstance(raw, dict):
            values = [raw['x'], raw['y'], raw.get('w', raw.get('width')), raw.get('h', raw.get('height'))]
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            values = str(raw).replace(';', ',').split(',')
        x, y, w, h = (float(v) for v in values)
    except (KeyError, TypeError, ValueError):
        raise ImageError('crop must be four numbers: x,y,width,height (fractions of the image)')
    eps = 1e-3
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0 \
            or x < -eps or y < -eps or x + w > 1 + eps or y + h > 1 + eps:
        raise ImageError('crop must lie inside the image (fractions between 0 and 1)')
    return x, y, w, h


def crop_box(width, height, crop, aspect=COVER_ASPECT):
    """Pixel box (left, top, right, bottom) for fractional crop on a width x height
    image, corrected to the exact aspect ratio around its centre (rounding,
    or a client that did not keep the ratio)."""
    x, y, w, h = crop
    left, top = x * width, y * height
    box_w, box_h = w * width, h * height
    if aspect:
        if box_w / box_h > aspect:
            box_w = box_h * aspect
        else:
            box_h = box_w / aspect
        left += (w * width - box_w) / 2
        top += (h * height - box_h) / 2
    box_w = max(min(box_w, width), min(MIN_CROP_PX, width))
    box_h = max(min(box_h, height), min(MIN_CROP_PX, height))
    left = min(max(0, left), width - box_w)
    top = min(max(0, top), height - box_h)
    box = (round(left), round(top), round(left + box_w), round(top + box_h))
    if aspect == 1:  # rounding must not make a square 1px off
        side = min(box[2] - box[0], box[3] - box[1])
        box = (box[0], box[1], box[0] + side, box[1] + side)
    return box


def _to_srgb(image):
    """Convert an embedded colour profile (Display P3, Adobe RGB, CMYK) to sRGB."""
    icc = image.info.get('icc_profile')
    if not icc:
        return image
    if image.mode == 'CMYK':
        mode = 'RGB'
    elif image.mode in ('RGBA', 'LA', 'PA'):
        image, mode = image.convert('RGBA'), 'RGBA'
    elif image.mode in ('RGB', 'L'):
        mode = image.mode
    else:
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        converted = ImageCms.profileToProfile(image, source, ImageCms.createProfile('sRGB'), outputMode=mode)
    except Exception:
        return image  # a broken profile is not worth rejecting the picture
    converted.info.pop('icc_profile', None)
    return converted


def _normalize_mode(image):
    """RGB, or RGBA when the image has transparency."""
    if image.mode in ('I;16', 'I;16B', 'I;16L', 'I;16N', 'I'):
        image = image.convert('I').point(lambda v: v * (1 / 256)).convert('L')
    elif image.mode == 'F':
        image = image.convert('L')
    elif image.mode == '1':
        image = image.convert('L')
    alpha = image.mode in ('RGBA', 'LA', 'PA', 'RGBa', 'La') or 'transparency' in image.info
    image = image.convert('RGBA' if alpha else 'RGB')
    if alpha and image.getextrema()[3][0] == 255:
        image = image.convert('RGB')  # alpha channel present but fully opaque
    return image


def open_image(data):
    """Upright sRGB RGB/RGBA image from uploaded bytes (first frame of animations)."""
    if not data:
        raise ImageError('empty image')
    if len(data) > MAX_INPUT_BYTES:
        raise ImageError(f'image is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB')
    try:
        image = Image.open(io.BytesIO(data))
        image.seek(0)
        image.load()
    except Image.DecompressionBombError:
        raise ImageError('image has too many pixels')
    except Exception:
        raise ImageError('not a readable image (supported: ' + ', '.join(FORMAT_NAMES) + ')')
    image = ImageOps.exif_transpose(image)
    image = _to_srgb(image)
    return _normalize_mode(image)


def encode(image, quality=90):
    """(mime, bytes): PNG when transparent, JPEG otherwise."""
    buffer = io.BytesIO()
    if image.mode == 'RGBA':
        image.save(buffer, 'PNG', optimize=True)
        return 'image/png', buffer.getvalue()
    image.save(buffer, 'JPEG', quality=quality, optimize=True)
    return 'image/jpeg', buffer.getvalue()


def prepare_cover(data, crop=None):
    """Cover ready for embedding: (mime, bytes, width, height).

    crop: fractions (x, y, w, h) of the upright image; the square is enforced.
    Without crop the whole image is kept (API compatibility). Never upscaled;
    shrunk to at most COVER_MAX_PX.
    """
    image = open_image(data)
    if crop is not None:
        image = image.crop(crop_box(image.width, image.height, crop))
    image.thumbnail((COVER_MAX_PX, COVER_MAX_PX), Image.LANCZOS)
    mime, encoded = encode(image)
    return mime, encoded, image.width, image.height


def preview(data, max_px=PREVIEW_MAX_PX):
    """Browser-displayable version of any supported image, for the cropper:
    (mime, bytes, width, height of the upright original)."""
    image = open_image(data)
    width, height = image.size
    image.thumbnail((max_px, max_px), Image.LANCZOS)
    mime, encoded = encode(image, quality=85)
    return mime, encoded, width, height
