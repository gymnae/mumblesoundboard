import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import library
import tag_writer
from sound_metadata import read_file
from library import (
    DuplicateError,
    LibraryError,
    SoundLibrary,
    gain_filter,
    is_visible_name,
    normalization_gain,
    sanitize_filename,
)

EXTENSIONS = ('mp3', 'wav', 'm4a', 'ogg')
HAS_FFMPEG = shutil.which('ffmpeg') is not None


def make_audio(path, freq=440, seconds=1.0, volume=None, codec=()):
    source = f'sine=f={freq}:d={seconds}'
    args = ['-af', f'volume={volume}dB'] if volume is not None else []
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', source, *args, *codec, path],
                   check=True)
    return path


class NameTests(unittest.TestCase):
    def test_sanitize_keeps_unicode_and_strips_paths(self):
        self.assertEqual(sanitize_filename('../../etc/Hallo Wält.MP3', EXTENSIONS), 'Hallo Wält.mp3')
        self.assertEqual(sanitize_filename('C:\\Users\\x\\a?b*c.wav', EXTENSIONS), 'abc.wav')
        self.assertEqual(sanitize_filename('...hidden.ogg', EXTENSIONS), 'hidden.ogg')
        self.assertEqual(sanitize_filename('  .mp3', EXTENSIONS), 'sound.mp3')
        self.assertEqual(sanitize_filename('a\x00b\nc.mp3', EXTENSIONS), 'abc.mp3')

    def test_sanitize_rejects_other_extensions(self):
        for name in ('evil.php', 'noext', 'x.mp3.exe'):
            with self.assertRaises(LibraryError) as ctx:
                sanitize_filename(name, EXTENSIONS)
            self.assertEqual(ctx.exception.status, 415)

    def test_visible_names(self):
        self.assertTrue(is_visible_name('a.mp3', EXTENSIONS))
        self.assertTrue(is_visible_name('LOUD.MP3', EXTENSIONS))
        for name in ('a.mp3.deleted-20260101T000000Z', '.upload-x.mp3', '.edit-y.wav', 'sub/a.mp3', 'a.txt', 'mp3', ''):
            self.assertFalse(is_visible_name(name, EXTENSIONS), name)


class GainTests(unittest.TestCase):
    def test_brings_loudness_to_target(self):
        self.assertEqual(normalization_gain(-20.0, -10.0, -16, 12, -1), (4.0, False))
        self.assertEqual(normalization_gain(-8.0, 0.5, -16, 12, -1), (-8.0, False))

    def test_caps_boost_and_requests_limiter_for_peaks(self):
        self.assertEqual(normalization_gain(-40.0, -20.0, -16, 12, -1), (12.0, False))
        self.assertEqual(normalization_gain(-22.0, -3.0, -16, 12, -1), (6.0, True))

    def test_silence_is_left_alone(self):
        self.assertEqual(normalization_gain(None, None, -16, 12, -1), (0.0, False))

    def test_filter_strings(self):
        self.assertIsNone(gain_filter(0.05, False, -1))
        self.assertEqual(gain_filter(-6.5, False, -1), 'volume=-6.50dB')
        self.assertEqual(gain_filter(6.0, True, -1), 'volume=6.00dB,alimiter=limit=0.8913:level=0')


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg is needed to create sample audio')
class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_tag_and_cover_edits_keep_the_fingerprint(self):
        for ext, codec in (('mp3', ()), ('wav', ()), ('m4a', ('-c:a', 'aac')), ('ogg', ('-c:a', 'libvorbis'))):
            with self.subTest(ext=ext):
                path = make_audio(os.path.join(self.tmp, 'a.' + ext), codec=codec)
                before = library.audio_fingerprint(path)
                self.assertRegex(before, r'^sha256:[0-9a-f]{64}$')
                tag_writer.write_fields(path, {'title': 'x' * 500, 'tags': ['a', 'b']})
                buffer = io.BytesIO()
                from PIL import Image
                Image.new('RGB', (400, 400), 'red').save(buffer, 'JPEG')
                tag_writer.write_cover(path, buffer.getvalue())
                self.assertEqual(library.audio_fingerprint(path), before)

    def test_different_audio_differs_and_junk_has_none(self):
        a = library.audio_fingerprint(make_audio(os.path.join(self.tmp, 'a.wav'), freq=440))
        b = library.audio_fingerprint(make_audio(os.path.join(self.tmp, 'b.wav'), freq=880))
        self.assertNotEqual(a, b)
        junk = os.path.join(self.tmp, 'junk.mp3')
        with open(junk, 'wb') as f:
            f.write(b'nope' * 100)
        self.assertIsNone(library.audio_fingerprint(junk))


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg is needed to create sample audio')
class LoudnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_measures_level_differences(self):
        loud = library.measure_loudness(make_audio(os.path.join(self.tmp, 'loud.wav'), seconds=2))
        quiet = library.measure_loudness(make_audio(os.path.join(self.tmp, 'quiet.wav'), seconds=2, volume=-12))
        self.assertAlmostEqual(loud[0] - quiet[0], 12.0, delta=0.3)
        self.assertAlmostEqual(loud[1] - quiet[1], 12.0, delta=0.3)

    def test_short_clip_is_measured_looped(self):
        long = library.measure_loudness(make_audio(os.path.join(self.tmp, 'long.wav'), seconds=2))
        short = library.measure_loudness(make_audio(os.path.join(self.tmp, 'short.wav'), seconds=0.15))
        self.assertIsNotNone(short[0])
        self.assertAlmostEqual(short[0], long[0], delta=0.5)

    def test_silence(self):
        path = os.path.join(self.tmp, 'silent.wav')
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anullsrc=d=1', path], check=True)
        self.assertEqual(library.measure_loudness(path), (None, None))


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg is needed to create sample audio')
class SoundLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sounds = os.path.join(self.tmp, 'sounds')
        self.samples = os.path.join(self.tmp, 'samples')
        os.makedirs(self.sounds)
        os.makedirs(self.samples)
        self.lib = self.new_library()

    def new_library(self):
        return SoundLibrary(self.sounds, os.path.join(self.tmp, 'library.db'), lambda: EXTENSIONS)

    def sample(self, name, **kwargs):
        return make_audio(os.path.join(self.samples, name), **kwargs)

    def upload(self, path, filename=None, max_bytes=10 * 1024 * 1024):
        with open(path, 'rb') as f:
            return self.lib.add_upload(f, filename or os.path.basename(path), max_bytes)

    def test_upload_stores_and_indexes(self):
        name = self.upload(self.sample('Air horn.mp3'))
        self.assertEqual(name, 'Air horn.mp3')
        self.assertEqual(self.lib.visible_names(), ['Air horn.mp3'])
        self.assertEqual(oct(os.stat(self.lib.path(name)).st_mode & 0o777), '0o644')
        self.assertIsNotNone(self.lib.entry(name)[0][2])

    def test_duplicate_audio_is_rejected_even_retagged_and_renamed(self):
        original = self.sample('horn.mp3')
        self.upload(original)
        copy = os.path.join(self.samples, 'copy.mp3')
        shutil.copy(original, copy)
        tag_writer.write_fields(copy, {'title': 'Totally different', 'tags': ['new']})
        with self.assertRaises(DuplicateError) as ctx:
            self.upload(copy, 'other name.mp3')
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.details, {'existing': 'horn.mp3'})
        self.assertEqual(self.lib.visible_names(), ['horn.mp3'])

    def test_duplicate_check_sees_files_added_by_hand(self):
        shutil.copy(self.sample('manual.wav'), os.path.join(self.sounds, 'manual.wav'))
        with self.assertRaises(DuplicateError):
            self.upload(os.path.join(self.samples, 'manual.wav'), 'again.wav')

    def test_same_name_different_audio_gets_suffix(self):
        self.upload(self.sample('a.wav', freq=440))
        self.assertEqual(self.upload(self.sample('b.wav', freq=880), 'a.wav'), 'a (2).wav')

    def test_rejects_non_audio_oversize_and_wrong_type(self):
        junk = os.path.join(self.samples, 'junk.mp3')
        with open(junk, 'wb') as f:
            f.write(os.urandom(4096))
        with self.assertRaises(LibraryError) as ctx:
            self.upload(junk)
        self.assertEqual(ctx.exception.status, 415)

        with self.assertRaises(LibraryError) as ctx:
            self.upload(self.sample('big.wav', seconds=3), max_bytes=1024)
        self.assertEqual(ctx.exception.status, 413)

        with self.assertRaises(LibraryError) as ctx:
            self.upload(self.sample('song.flac'))
        self.assertEqual(ctx.exception.status, 415)

        self.assertEqual(os.listdir(self.sounds), [])  # no temp files left behind

    def test_concurrent_identical_uploads_store_one(self):
        path = self.sample('race.wav')
        results = []

        def go(i):
            try:
                results.append(self.upload(path, f'race{i}.wav'))
            except DuplicateError:
                results.append('dup')

        threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count('dup'), 3)
        self.assertEqual(len(self.lib.visible_names()), 1)

    def test_delete_masks_and_restore_brings_back(self):
        name = self.upload(self.sample('bye.mp3'))
        masked = self.lib.delete(name)
        self.assertRegex(masked, r'^bye\.mp3\.deleted-\d{8}T\d{6}Z$')
        self.assertTrue(os.path.isfile(self.lib.path(masked)))  # nothing erased
        self.assertEqual(self.lib.visible_names(), [])
        self.assertEqual([item['name'] for item in self.lib.deleted()], ['bye.mp3'])

        # Deleted audio does not block a fresh upload ...
        self.assertEqual(self.upload(os.path.join(self.samples, 'bye.mp3'), 'again.mp3'), 'again.mp3')
        # ... but then restoring it would create a duplicate.
        with self.assertRaises(DuplicateError):
            self.lib.restore(masked)
        self.lib.delete('again.mp3')
        self.assertEqual(self.lib.restore(masked), 'bye.mp3')
        self.assertEqual(self.lib.visible_names(), ['bye.mp3'])

    def test_deleting_the_same_name_twice_in_one_second(self):
        path = self.sample('twice.wav')
        self.upload(path)
        first = self.lib.delete('twice.wav')
        shutil.copy(path, self.lib.path('twice.wav'))
        with mock.patch('library.time.strftime', return_value=first.rsplit('-', 1)[1]):
            second = self.lib.delete('twice.wav')
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.lib.deleted()), 2)

    def test_delete_and_restore_refuse_bad_names(self):
        for name in ('missing.mp3', '../x.mp3', '.upload-a.mp3'):
            with self.assertRaises(LibraryError):
                self.lib.delete(name)
        for masked in ('x.mp3', 'missing.mp3.deleted-20260101T000000Z', '../a.mp3.deleted-20260101T000000Z'):
            with self.assertRaises(LibraryError):
                self.lib.restore(masked)

    def test_restore_refuses_when_name_is_taken(self):
        self.upload(self.sample('taken.wav', freq=300))
        masked = self.lib.delete('taken.wav')
        self.upload(self.sample('other.wav', freq=600), 'taken.wav')
        with self.assertRaises(LibraryError) as ctx:
            self.lib.restore(masked)
        self.assertEqual(ctx.exception.status, 409)

    def test_normalization_filter_after_analysis(self):
        name = self.upload(self.sample('quiet.wav', seconds=2, volume=-6))
        self.assertIsNone(self.lib.playback_filter(name))  # not analyzed yet
        self.assertEqual(self.lib.loudness_info(name)['status'], 'pending')
        self.lib.analyze_pending()
        info = self.lib.loudness_info(name)
        self.assertEqual(info['status'], 'analyzed')
        self.assertAlmostEqual(info['gain_db'], -16 - info['lufs'], delta=0.1)
        self.assertGreater(info['gain_db'], 5)
        self.assertFalse(info['limiter'])
        self.assertTrue(self.lib.playback_filter(name).startswith('volume='))

        # Retagging keeps the analysis (same audio), and a fresh instance loads it from SQLite.
        tag_writer.write_fields(self.lib.path(name), {'title': 'Q'})
        self.lib.file_changed(name)
        self.assertIsNotNone(self.lib.playback_filter(name))
        self.assertEqual(self.new_library().loudness_info(name), self.lib.loudness_info(name))

    def test_changed_file_is_not_normalized_with_stale_data(self):
        name = self.upload(self.sample('change.wav', seconds=2, volume=-20))
        self.lib.analyze_pending()
        make_audio(self.lib.path(name), freq=1000, seconds=2.5)
        self.assertIsNone(self.lib.playback_filter(name))
        self.lib.sync()
        self.lib.analyze_pending()
        self.assertIsNotNone(self.lib.playback_filter(name))

    def test_normalization_can_be_disabled(self):
        self.lib.normalize = False
        name = self.upload(self.sample('off.wav', volume=-20))
        self.lib.analyze_pending()
        self.assertIsNone(self.lib.playback_filter(name))
        self.assertEqual(self.lib.loudness_info(name)['gain_db'], 0.0)

    def test_sync_forgets_vanished_files(self):
        name = self.upload(self.sample('gone.wav'))
        os.unlink(self.lib.path(name))
        self.lib.sync()
        self.assertIsNone(self.lib.entry(name))


if __name__ == '__main__':
    unittest.main()


def duration(path):
    return library.waveform(path, 1)['duration']


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg is needed')
class TrimTests(unittest.TestCase):
    FORMATS = {  # extension -> encoder args for the sample
        'wav': (),
        'mp3': ('-c:a', 'libmp3lame'),
        'm4a': ('-c:a', 'aac'),
        'ogg': ('-c:a', 'libvorbis'),
        'opus': ('-c:a', 'libopus'),
        'flac': ('-c:a', 'flac'),
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sounds = os.path.join(self.tmp, 'sounds')
        os.makedirs(self.sounds)
        self.lib = SoundLibrary(self.sounds, os.path.join(self.tmp, 'library.db'),
                                lambda: tuple(self.FORMATS))

    def sound(self, name, seconds=2.0, freq=440, **fields):
        ext = name.rsplit('.', 1)[1]
        path = make_audio(os.path.join(self.sounds, name), freq=freq, seconds=seconds, codec=self.FORMATS[ext])
        if fields:
            tag_writer.write_fields(path, tag_writer.validate_fields(fields))
        self.lib.sync()
        return path

    def cover(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), (200, 30, 30)).save(buffer, 'PNG')
        return buffer.getvalue()

    def test_trim_cuts_keeps_tags_cover_and_a_backup(self):
        for ext in self.FORMATS:
            with self.subTest(ext=ext):
                name = f'horn.{ext}'
                path = self.sound(name, title='Air Horn', tags=['loud', 'meme'])
                tag_writer.write_cover(path, self.cover())
                before = self.lib.entry(name)[0][2]

                backup = self.lib.trim(name, 0.5, 1.5, fade_in=0.1, fade_out=0.1)

                self.assertAlmostEqual(duration(path), 1.0, delta=0.06)
                info, cover = read_file(path)
                self.assertEqual((info['title'], info['tags']), ('Air Horn', ['loud', 'meme']))
                self.assertIsNotNone(cover)
                self.assertAlmostEqual(info['duration'], 1.0, delta=0.1)  # what the tiles show
                self.assertEqual(self.lib.trim_backups(name), [backup])
                self.assertAlmostEqual(duration(self.lib.path(backup)), 2.0, delta=0.06)
                self.assertNotIn(backup, self.lib.visible_names())
                self.assertNotEqual(self.lib.entry(name)[0][2], before)  # re-fingerprinted

    def test_fades_start_and_end_quietly(self):
        path = self.sound('fade.wav')
        self.lib.trim('fade.wav', 0.0, 1.0, fade_in=0.5, fade_out=0.5)
        peaks = library.waveform(path, 100)['peaks']
        self.assertLess(peaks[0], 0.02)   # the sine peaks at 0.125
        self.assertLess(peaks[-1], 0.02)
        self.assertGreater(max(peaks), 0.11)

    def test_rejects_bad_selections(self):
        self.sound('a.wav')
        for start, end, fin, fout in ((1, 1, 0, 0), (1.5, 1.0, 0, 0), (-1, 1, 0, 0), ('x', 1, 0, 0),
                                      (0, 1, 0.6, 0.6), (0, 1, -0.1, 0), (5, 6, 0, 0)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(LibraryError) as ctx:
                    self.lib.trim('a.wav', start, end, fin, fout)
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.lib.trim_backups('a.wav'), [])
        with self.assertRaises(LibraryError) as ctx:
            self.lib.trim('nope.wav', 0, 1)
        self.assertEqual(ctx.exception.status, 404)

    def test_untrim_brings_back_the_previous_version_with_current_tags(self):
        path = self.sound('horn.mp3', title='Old')
        original = self.lib.entry('horn.mp3')[0][2]
        self.lib.trim('horn.mp3', 0.2, 1.8)
        self.lib.trim('horn.mp3', 0.2, 1.0)
        self.assertEqual(len(self.lib.trim_backups('horn.mp3')), 2)
        self.assertEqual(self.lib.trim_counts(), {'horn.mp3': 2})
        tag_writer.write_fields(path, {'title': 'New'})

        masked = self.lib.untrim('horn.mp3')
        self.assertAlmostEqual(duration(path), 1.6, delta=0.06)
        self.assertEqual(self.lib.untrim('horn.mp3'), self.lib.deleted()[0]['id'])
        self.assertAlmostEqual(duration(path), 2.0, delta=0.06)
        self.assertEqual(self.lib.entry('horn.mp3')[0][2], original)
        self.assertEqual(read_file(path)[0]['title'], 'New')
        self.assertEqual(self.lib.trim_backups('horn.mp3'), [])
        trashed = [item['id'] for item in self.lib.deleted()]
        self.assertIn(masked, trashed)
        self.assertEqual(len(trashed), 2)  # both trimmed versions are kept as deleted sounds
        with self.assertRaises(LibraryError) as ctx:
            self.lib.untrim('horn.mp3')
        self.assertEqual(ctx.exception.status, 404)

    def test_untrim_refuses_when_the_untrimmed_audio_exists_again(self):
        self.sound('a.wav')
        self.lib.trim('a.wav', 0, 1)
        shutil.copy(self.lib.path(self.lib.trim_backups('a.wav')[0]), self.lib.path('copy.wav'))
        with self.assertRaises(DuplicateError) as ctx:
            self.lib.untrim('a.wav')
        self.assertEqual(ctx.exception.details['existing'], 'copy.wav')

    def test_backups_follow_delete_and_restore(self):
        self.sound('a.wav')
        self.lib.trim('a.wav', 0, 1)
        masked = self.lib.delete('a.wav')
        self.assertEqual(self.lib.trim_backups('a.wav'), [])
        self.assertEqual([item['id'] for item in self.lib.deleted()], [masked])  # backups are not listed
        self.sound('a.wav', freq=880)  # a different sound takes the name meanwhile
        self.assertEqual(self.lib.trim_backups('a.wav'), [])
        os.rename(self.lib.path('a.wav'), self.lib.path('other.wav'))
        self.lib.sync()
        self.lib.restore(masked)
        self.assertEqual(len(self.lib.trim_backups('a.wav')), 1)

    def test_waveform(self):
        self.sound('a.wav', seconds=1.0)
        wave = self.lib.waveform('a.wav', 200)
        self.assertAlmostEqual(wave['duration'], 1.0, delta=0.01)
        self.assertEqual(len(wave['peaks']), 200)
        self.assertTrue(all(0 <= p <= 1 for p in wave['peaks']))
