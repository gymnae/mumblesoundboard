import io
import unittest

from PIL import Image, ImageCms

import cover_image
from cover_image import ImageError, crop_box, parse_crop, prepare_cover, preview


def encode(image, fmt, **kwargs):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def decoded(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def quadrants(width=400, height=200):
    """Red | green on top, blue | white below: tells where a crop came from."""
    image = Image.new('RGB', (width, height))
    image.paste((255, 0, 0), (0, 0, width // 2, height // 2))
    image.paste((0, 255, 0), (width // 2, 0, width, height // 2))
    image.paste((0, 0, 255), (0, height // 2, width // 2, height))
    image.paste((255, 255, 255), (width // 2, height // 2, width, height))
    return image


def near(color, expected, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(color, expected))


class ParseCropTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(parse_crop('0.1,0.2,0.5,0.5'), (0.1, 0.2, 0.5, 0.5))
        self.assertEqual(parse_crop([0, 0, 1, 1]), (0, 0, 1, 1))
        self.assertEqual(parse_crop({'x': 0, 'y': 0.5, 'width': 0.5, 'height': 0.5}), (0, 0.5, 0.5, 0.5))
        self.assertIsNone(parse_crop(''))
        self.assertIsNone(parse_crop(None))

    def test_rejected(self):
        for raw in ('1,2,3', 'a,b,c,d', '0,0,0,0.5', '0.6,0,0.5,0.5', '-0.2,0,0.5,0.5', 'nan,0,1,1', {'x': 1}):
            with self.assertRaises(ImageError, msg=raw):
                parse_crop(raw)


class CropBoxTests(unittest.TestCase):
    def test_exact_square(self):
        self.assertEqual(crop_box(400, 200, (0.5, 0, 0.5, 1)), (200, 0, 400, 200))

    def test_non_square_request_is_squared_around_its_centre(self):
        # 0..1 x 0..1 of a 400x200 image: the 200x200 square in the middle
        self.assertEqual(crop_box(400, 200, (0, 0, 1, 1)), (100, 0, 300, 200))

    def test_rounding_never_leaves_the_image_or_the_square(self):
        for crop in ((0.333333, 0.1, 0.333333, 0.666667), (0.999, 0.999, 0.001, 0.002)):
            left, top, right, bottom = crop_box(1001, 667, crop)
            self.assertEqual(right - left, bottom - top)
            self.assertTrue(0 <= left < right <= 1001 and 0 <= top < bottom <= 667)

    def test_tiny_selection_grows_to_the_minimum(self):
        left, top, right, bottom = crop_box(1000, 1000, (0.5, 0.5, 0.001, 0.001))
        self.assertEqual(right - left, cover_image.MIN_CROP_PX)


class PrepareCoverTests(unittest.TestCase):
    def test_crop_picks_the_selected_area(self):
        data = encode(quadrants(), 'PNG')
        mime, cover, width, height = prepare_cover(data, (0.5, 0.5, 0.25, 0.5))  # bottom-right 100x100
        self.assertEqual((mime, width, height), ('image/jpeg', 100, 100))
        self.assertTrue(near(decoded(cover).getpixel((50, 50)), (255, 255, 255)))

        _, cover, _, _ = prepare_cover(data, (0, 0, 0.25, 0.5))  # top-left
        self.assertTrue(near(decoded(cover).getpixel((50, 50)), (255, 0, 0)))

    def test_without_crop_the_whole_image_is_kept(self):
        _, _, width, height = prepare_cover(encode(quadrants(), 'PNG'))
        self.assertEqual((width, height), (400, 200))

    def test_large_selection_is_shrunk_never_enlarged(self):
        big = encode(Image.new('RGB', (3000, 2000), 'red'), 'JPEG')
        self.assertEqual(prepare_cover(big, (0, 0, 2 / 3, 1))[2:], (1000, 1000))
        small = encode(Image.new('RGB', (300, 200), 'red'), 'JPEG')
        self.assertEqual(prepare_cover(small, (0, 0, 2 / 3, 1))[2:], (200, 200))

    def test_many_formats_are_converted(self):
        image = quadrants(200, 200)
        inputs = {
            'BMP': encode(image, 'BMP'),
            'TIFF': encode(image, 'TIFF', compression='tiff_lzw'),
            'GIF': encode(image, 'GIF'),
            'WEBP': encode(image, 'WEBP', lossless=True),
            'ICO': encode(image, 'ICO', sizes=[(128, 128)]),
            'TGA': encode(image, 'TGA'),
            'PPM': encode(image, 'PPM'),
            'QOI': encode(image, 'QOI'),
            'JPEG2000': encode(image, 'JPEG2000'),
            'AVIF': encode(image, 'AVIF', quality=90),
        }
        if cover_image.HEIF_SUPPORT:
            import pillow_heif
            heif = pillow_heif.from_pillow(image)
            buffer = io.BytesIO()
            heif.save(buffer, format='HEIF', quality=95)
            inputs['HEIC'] = buffer.getvalue()
        for name, data in inputs.items():
            with self.subTest(format=name):
                mime, cover, width, height = prepare_cover(data, (0, 0, 0.5, 0.5))
                self.assertEqual(mime, 'image/jpeg')
                self.assertEqual(width, height)
                self.assertTrue(near(decoded(cover).getpixel((width // 2, height // 2)), (255, 0, 0)),
                                decoded(cover).getpixel((width // 2, height // 2)))

    def test_animated_gif_uses_the_first_frame(self):
        frames = [Image.new('RGB', (64, 64), color) for color in ('red', 'blue')]
        data = encode(frames[0], 'GIF', save_all=True, append_images=frames[1:], duration=100, loop=0)
        _, cover, _, _ = prepare_cover(data, (0, 0, 1, 1))
        self.assertTrue(near(decoded(cover).getpixel((32, 32)), (255, 0, 0)))

    def test_exif_rotation_is_applied_before_cropping(self):
        # Stored landscape (left red, right blue) with orientation 6 = rotate 90° clockwise:
        # upright it is portrait with red on top.
        stored = Image.new('RGB', (200, 100), 'blue')
        stored.paste((255, 0, 0), (0, 0, 100, 100))
        exif = Image.Exif()
        exif[0x0112] = 6
        data = encode(stored, 'JPEG', exif=exif, quality=95)
        mime, cover, width, height = prepare_cover(data, (0, 0, 1, 0.5))  # top square of the upright image
        self.assertEqual((width, height), (100, 100))
        self.assertTrue(near(decoded(cover).getpixel((50, 50)), (255, 0, 0)))

    def test_transparency_stays_png_and_opaque_alpha_becomes_jpeg(self):
        clear = encode(Image.new('RGBA', (50, 50), (255, 0, 0, 0)), 'PNG')
        self.assertEqual(prepare_cover(clear)[0], 'image/png')
        palette = Image.new('P', (50, 50), 0)
        palette.info['transparency'] = 0
        self.assertEqual(prepare_cover(encode(palette, 'PNG'))[0], 'image/png')
        opaque = encode(Image.new('RGBA', (50, 50), (255, 0, 0, 255)), 'PNG')
        self.assertEqual(prepare_cover(opaque)[0], 'image/jpeg')

    def test_unusual_modes(self):
        sixteen = Image.new('I;16', (40, 40), 65535)
        _, cover, _, _ = prepare_cover(encode(sixteen, 'PNG'))
        self.assertTrue(near(decoded(cover).getpixel((20, 20)), (255, 255, 255)))
        cmyk = Image.new('CMYK', (40, 40), (0, 255, 255, 0))  # red
        _, cover, _, _ = prepare_cover(encode(cmyk, 'JPEG', quality=95))
        self.assertTrue(near(decoded(cover).getpixel((20, 20)), (255, 0, 0), tolerance=60))
        _, cover, _, _ = prepare_cover(encode(Image.new('1', (40, 40), 1), 'BMP'))
        self.assertTrue(near(decoded(cover).getpixel((20, 20)), (255, 255, 255)))

    def test_colour_profile_is_converted_to_srgb(self):
        # Pillow can only build sRGB/LAB/XYZ profiles itself: check the profile is
        # applied and dropped (colours unchanged for an sRGB source).
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
        data = encode(Image.new('RGB', (20, 20), (200, 100, 50)), 'JPEG', quality=95,
                      icc_profile=srgb.tobytes())
        _, cover, _, _ = prepare_cover(data)
        self.assertTrue(near(decoded(cover).getpixel((10, 10)), (200, 100, 50), tolerance=10))
        self.assertIsNone(decoded(cover).info.get('icc_profile'))

    def test_broken_profile_is_ignored(self):
        data = encode(Image.new('RGB', (20, 20), 'red'), 'JPEG', icc_profile=b'not a profile')
        self.assertEqual(prepare_cover(data)[0], 'image/jpeg')

    def test_rejects(self):
        with self.assertRaises(ImageError):
            prepare_cover(b'')
        with self.assertRaises(ImageError):
            prepare_cover(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with self.assertRaises(ImageError):
            prepare_cover(b'\0' * (cover_image.MAX_INPUT_BYTES + 1))


class PreviewTests(unittest.TestCase):
    def test_preview_is_small_but_reports_the_original_size(self):
        data = encode(Image.new('RGB', (4000, 3000), 'red'), 'TIFF', compression='tiff_lzw')
        mime, image, width, height = preview(data)
        self.assertEqual((mime, width, height), ('image/jpeg', 4000, 3000))
        self.assertEqual(decoded(image).size, (1600, 1200))


if __name__ == '__main__':
    unittest.main()
