import io
import unittest

from PIL import Image

from artwork import render_frame


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
