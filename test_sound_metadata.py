import base64
import os
import shutil
import subprocess
import tempfile
import unittest

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TCON, TIT2, TPE1
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis

from sound_metadata import MetadataCache, read_file

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
)
JPEG = b'\xff\xd8\xff\xe0fake-jpeg'


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is needed to create sample audio')
class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def make(self, name, *codec):
        path = os.path.join(self.tmp, name)
        subprocess.run(
            ['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
             '-t', '1', *codec, path],
            check=True,
        )
        return path

    def test_mp3_tags_and_front_cover_preferred(self):
        path = self.make('a.mp3')
        tags = ID3()
        tags.add(TIT2(encoding=3, text='Song'))
        tags.add(TPE1(encoding=3, text='Band'))
        tags.add(TCON(encoding=3, text='(17)'))
        tags.add(APIC(encoding=3, mime='image/jpeg', type=4, desc='back', data=JPEG))
        tags.add(APIC(encoding=3, mime='image/png', type=3, desc='front', data=PNG))
        tags.save(path)

        meta, cover = read_file(path)

        self.assertEqual(meta['title'], 'Song')
        self.assertEqual(meta['artist'], 'Band')
        self.assertEqual(meta['genre'], 'Rock')
        self.assertAlmostEqual(meta['duration'], 1.0, delta=0.1)
        self.assertEqual(cover, ('image/png', PNG))

    def test_mp3_without_tags_has_no_cover(self):
        meta, cover = read_file(self.make('plain.mp3'))
        self.assertIsNone(meta['title'])
        self.assertIsNone(cover)

    def test_m4a_cover(self):
        path = self.make('a.m4a', '-c:a', 'aac')
        audio = MP4(path)
        audio['\xa9nam'] = ['Tune']
        audio['covr'] = [MP4Cover(PNG, imageformat=MP4Cover.FORMAT_PNG)]
        audio.save()

        meta, cover = read_file(path)

        self.assertEqual(meta['title'], 'Tune')
        self.assertEqual(cover, ('image/png', PNG))

    def test_flac_picture(self):
        path = self.make('a.flac')
        audio = FLAC(path)
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, 'image/jpeg', JPEG
        audio.add_picture(pic)
        audio['album'] = 'Record'
        audio.save()

        meta, cover = read_file(path)

        self.assertEqual(meta['album'], 'Record')
        self.assertEqual(cover, ('image/jpeg', JPEG))

    def test_ogg_metadata_block_picture(self):
        path = self.make('a.ogg', '-c:a', 'libvorbis')
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, '', PNG  # missing mime is sniffed
        audio = OggVorbis(path)
        audio['metadata_block_picture'] = [base64.b64encode(pic.write()).decode()]
        audio['date'] = '1998'
        audio.save()

        meta, cover = read_file(path)

        self.assertEqual(meta['year'], '1998')
        self.assertEqual(cover, ('image/png', PNG))

    def test_unreadable_file(self):
        path = os.path.join(self.tmp, 'junk.mp3')
        with open(path, 'wb') as f:
            f.write(b'not audio')
        meta, cover = read_file(path)
        self.assertIsNone(meta['title'])
        self.assertIsNone(cover)

    def test_cache_invalidates_on_change(self):
        path = self.make('c.mp3')
        cache = MetadataCache()
        self.assertFalse(cache.metadata(path)['has_cover'])
        self.assertIsNone(cache.cover(path))

        tags = ID3()
        tags.add(APIC(encoding=3, mime='image/png', type=3, data=PNG))
        tags.save(path)
        os.utime(path, ns=(1, 1))

        self.assertTrue(cache.metadata(path)['has_cover'])
        mime, data, stamp = cache.cover(path)
        self.assertEqual((mime, data), ('image/png', PNG))
        self.assertEqual(stamp[0], 1)


if __name__ == '__main__':
    unittest.main()
