import io
import os
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image

import tag_writer
from sound_metadata import normalize_tags, read_file

FORMATS = {
    'mp3': [],
    'wav': [],
    'm4a': ['-c:a', 'aac'],
    'ogg': ['-c:a', 'libvorbis'],
    'opus': ['-c:a', 'libopus'],
    'flac': [],
}


def image_bytes(fmt='PNG', size=(1600, 900), mode='RGB'):
    buffer = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 255) if mode == 'RGBA' else (200, 30, 30)).save(buffer, fmt)
    return buffer.getvalue()


class NormalizeTagsTests(unittest.TestCase):
    def test_cleans_and_splits(self):
        self.assertEqual(
            normalize_tags(['  Airhorn ', '#MEME; meme', 'star wars, Star  Wars', '', '#']),
            ['airhorn', 'meme', 'star-wars'],
        )

    def test_limits(self):
        self.assertEqual(len(normalize_tags([f't{i}' for i in range(80)])), 50)
        self.assertEqual(len(normalize_tags(['x' * 99])[0]), 40)


class ValidateFieldsTests(unittest.TestCase):
    def test_cleans_values(self):
        cleaned = tag_writer.validate_fields({'title': '  Air   horn ', 'year': '1998', 'artist': '', 'tags': 'a, b'})
        self.assertEqual(cleaned, {'title': 'Air horn', 'year': '1998', 'artist': None, 'tags': ['a', 'b']})

    def test_absent_fields_stay_untouched(self):
        self.assertEqual(tag_writer.validate_fields({'genre': 'Fx'}), {'genre': 'Fx'})

    def test_rejects_bad_input(self):
        for bad in (['x'], {'nope': 1}, {'year': 'last year'}, {'title': 'x' * 201}, {'tags': [1, 2]}, {'title': {}}):
            with self.assertRaises(tag_writer.TagWriteError, msg=bad):
                tag_writer.validate_fields(bad)


class PrepareCoverTests(unittest.TestCase):
    def test_shrinks_opaque_image_to_jpeg(self):
        mime, data, width, height = tag_writer.prepare_cover(image_bytes())
        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual((width, height), (1000, 563))
        self.assertTrue(data.startswith(b'\xff\xd8'))

    def test_keeps_transparency_as_png(self):
        mime, data, width, height = tag_writer.prepare_cover(image_bytes(size=(64, 64), mode='RGBA'))
        self.assertEqual((mime, width, height), ('image/png', 64, 64))

    def test_rejects_non_images(self):
        with self.assertRaises(tag_writer.TagWriteError):
            tag_writer.prepare_cover(b'definitely not an image')


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is needed to create sample audio')
class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def make(self, ext):
        path = os.path.join(self.tmp, 'sound.' + ext)
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=f=440:d=1',
                        *FORMATS[ext], path], check=True)
        return path

    def test_fields_and_tags_round_trip_in_every_format(self):
        for ext in FORMATS:
            with self.subTest(ext=ext):
                path = self.make(ext)
                tag_writer.write_fields(path, tag_writer.validate_fields({
                    'title': 'Airhorn', 'artist': 'MLG', 'album': 'Memes', 'year': '2014',
                    'genre': 'Effects', 'tags': ['Loud', 'meme', 'star wars'],
                }))
                meta, _ = read_file(path)
                self.assertEqual(
                    {k: meta[k] for k in ('title', 'artist', 'album', 'year', 'genre', 'tags')},
                    {'title': 'Airhorn', 'artist': 'MLG', 'album': 'Memes', 'year': '2014',
                     'genre': 'Effects', 'tags': ['loud', 'meme', 'star-wars']},
                )

                # Clearing a field and the tags removes them; others stay.
                tag_writer.write_fields(path, tag_writer.validate_fields({'album': '', 'tags': []}))
                meta, _ = read_file(path)
                self.assertIsNone(meta['album'])
                self.assertEqual(meta['tags'], [])
                self.assertEqual(meta['title'], 'Airhorn')
                self.assertAlmostEqual(meta['duration'], 1.0, delta=0.15)

    def test_cover_set_and_removed_in_every_format(self):
        for ext in FORMATS:
            with self.subTest(ext=ext):
                path = self.make(ext)
                tag_writer.write_cover(path, image_bytes(size=(300, 300)))
                _, cover = read_file(path)
                self.assertIsNotNone(cover)
                self.assertEqual(cover[0], 'image/jpeg')
                self.assertEqual(Image.open(io.BytesIO(cover[1])).size, (300, 300))

                # A second cover replaces the first rather than adding one
                tag_writer.write_cover(path, image_bytes(size=(64, 64), mode='RGBA'))
                _, cover = read_file(path)
                self.assertEqual(cover[0], 'image/png')

                tag_writer.write_cover(path, None)
                self.assertIsNone(read_file(path)[1])

    def test_replaces_atomically_and_leaves_no_temp_files(self):
        path = self.make('mp3')
        with open(path, 'rb') as playing:  # like an ffmpeg process mid-playback
            before = playing.read()
            tag_writer.write_fields(path, {'title': 'New'})
            playing.seek(0)
            self.assertEqual(playing.read(), before)  # the old inode is untouched
        self.assertEqual(os.listdir(self.tmp), ['sound.mp3'])

    def test_unreadable_file_raises_and_keeps_original(self):
        path = os.path.join(self.tmp, 'junk.mp3')
        with open(path, 'wb') as f:
            f.write(b'not audio at all')
        with self.assertRaises(tag_writer.TagWriteError):
            tag_writer.write_fields(path, {'title': 'x'})
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'not audio at all')
        self.assertEqual(os.listdir(self.tmp), ['junk.mp3'])


if __name__ == '__main__':
    unittest.main()
