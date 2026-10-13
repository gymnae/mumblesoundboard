import io
import unittest

from PIL import Image

from artwork import render_frame, thumbnail


def encoded(width, height, color=(200, 50, 120)):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buffer, 'PNG')
    return buffer.getvalue()


def frame_image(rgba, width=640, height=360):
    return Image.frombytes('RGBA', (width, height), rgba)


class RenderFrameTests(unittest.TestCase):
    def test_frame_has_rgba_size(self):
        self.assertEqual(len(render_frame(None, 'x', width=320, height=180)), 320 * 180 * 4)

    def test_square_cover_sits_left_of_text(self):
        img = frame_image(render_frame(encoded(300, 300), 'Song', 'Artist'))

        self.assertEqual(img.getpixel((180, 180))[:3], (200, 50, 120))  # cover box
        self.assertEqual(img.getpixel((630, 340))[:3], (0, 0, 128))  # text area background

    def test_wide_thumbnail_fills_frame_above_title_bar(self):
        img = frame_image(render_frame(encoded(1280, 720, (30, 120, 200)), 'Video', 'host'))

        self.assertEqual(img.getpixel((600, 40))[:3], (30, 120, 200))
        self.assertEqual(img.getpixel((600, 350))[:3], (0, 0, 128))

    def test_broken_image_uses_default_cover(self):
        self.assertEqual(render_frame(b'not an image', 'x'), render_frame(None, 'x'))

    def test_long_and_empty_text_render(self):
        render_frame(None, 'word ' * 200, 'sub ' * 100)
        render_frame(encoded(1280, 720), '', '   ')


if __name__ == '__main__':
    unittest.main()


class ThumbnailTests(unittest.TestCase):
    def test_large_cover_shrinks_to_fit_keeping_aspect(self):
        mime, data = thumbnail(encoded(1200, 600), 72)

        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(data)).size, (72, 36))

    def test_small_cover_is_not_enlarged(self):
        _, data = thumbnail(encoded(40, 40), 72)

        self.assertEqual(Image.open(io.BytesIO(data)).size, (40, 40))

    def test_transparent_cover_stays_png(self):
        buffer = io.BytesIO()
        Image.new('RGBA', (300, 300), (0, 0, 0, 0)).save(buffer, 'PNG')
        mime, data = thumbnail(buffer.getvalue(), 64)

        self.assertEqual(mime, 'image/png')
        self.assertEqual(Image.open(io.BytesIO(data)).mode, 'RGBA')

    def test_missing_or_broken_art_gives_default_at_requested_size(self):
        for art in (None, b'not an image'):
            mime, data = thumbnail(art, 96)
            self.assertEqual(mime, 'image/png')
            self.assertEqual(Image.open(io.BytesIO(data)).size, (96, 96))
