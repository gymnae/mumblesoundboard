import asyncio
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from artwork import Artwork
from meet_bot import (
    AUDIO_BYTES_PER_FRAME,
    VIDEO_OFF_DELAY_SECONDS,
    ArtworkVideo,
    MeetBot,
    copy_pcm_to_frame,
)


class FakeAudioFrame:
    """Models LiveKit's writable, typed int16 AudioFrame.data view."""

    def __init__(self, byte_count=AUDIO_BYTES_PER_FRAME):
        self._storage = bytearray(byte_count)
        self.data = memoryview(self._storage).cast('h')


class CopyPcmToFrameTests(unittest.TestCase):
    def test_copies_pcm_into_typed_frame_view(self):
        pcm = bytes(index % 256 for index in range(AUDIO_BYTES_PER_FRAME))
        frame = FakeAudioFrame()

        copy_pcm_to_frame(frame, pcm)

        self.assertEqual(frame.data.cast('B').tobytes(), pcm)

    def test_rejects_malformed_pcm_length(self):
        frame = FakeAudioFrame()

        with self.assertRaisesRegex(
            ValueError,
            r"expected 1920 PCM bytes, got 1918",
        ):
            copy_pcm_to_frame(frame, bytes(AUDIO_BYTES_PER_FRAME - 2))

    def test_rejects_unexpected_frame_capacity(self):
        frame = FakeAudioFrame(AUDIO_BYTES_PER_FRAME - 2)

        with self.assertRaisesRegex(
            ValueError,
            r"LiveKit frame has 1918 bytes, expected 1920",
        ):
            copy_pcm_to_frame(frame, bytes(AUDIO_BYTES_PER_FRAME))


class MeetBotTokenTests(unittest.TestCase):
    def make_bot(self):
        with mock.patch.dict(os.environ, {'MEET_URL': 'https://schnackn.example'}, clear=True):
            bot = MeetBot(mock.Mock(), 'SoundBot')
        bot.room_name = ' Protected Room! '
        return bot

    def test_request_token_sends_optional_password(self):
        bot = self.make_bot()
        bot.password = 'room-secret'
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'token': 'join-token',
            'serverUrl': 'wss://livekit.example',
        }).encode()

        with mock.patch('urllib.request.urlopen', return_value=response) as urlopen:
            token, server_url = bot._request_token()

        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {
            'roomName': 'protectedroom',
            'nickname': 'SoundBot',
            'password': 'room-secret',
        })
        self.assertEqual((token, server_url), ('join-token', 'wss://livekit.example'))

    def test_request_token_reports_protected_room(self):
        bot = self.make_bot()
        bot.password = ''
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"requiresPassword": true}'

        with mock.patch('urllib.request.urlopen', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'Room requires a password'):
                bot._request_token()

    def test_request_token_reports_wrong_password(self):
        bot = self.make_bot()
        error = urllib.error.HTTPError(
            bot.meet_base_url + '/api/token',
            401,
            'Unauthorized',
            {},
            io.BytesIO(b'{"error": "Incorrect Password"}'),
        )

        with mock.patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'Wrong room password'):
                bot._request_token()


class MeetBotIdleTimeoutTests(unittest.TestCase):
    def make_bot(self, name='SoundBot'):
        with mock.patch.dict(os.environ, {
            'MEET_URL': 'https://schnackn.example',
            'MEET_IDLE_TIMEOUT_SECONDS': '3600',
        }, clear=True):
            return MeetBot(mock.Mock(), name)

    def connect_fake_session(self, bot, last_audio_at=100.0):
        bot._loop = mock.Mock()
        bot._queue = mock.Mock()
        bot.connected = True
        bot._last_audio_at = last_audio_at

    def test_times_out_one_hour_after_last_audio(self):
        bot = self.make_bot()
        self.connect_fake_session(bot)

        self.assertFalse(bot._idle_timed_out(now=3699.999))
        self.assertTrue(bot._idle_timed_out(now=3700.0))

    def test_feed_refreshes_activity_for_connected_bot(self):
        bot = self.make_bot()
        self.connect_fake_session(bot)

        with mock.patch('meet_bot.time.monotonic', return_value=500.0):
            bot.feed(bytes(AUDIO_BYTES_PER_FRAME))

        self.assertEqual(bot._last_audio_at, 500.0)
        bot._loop.call_soon_threadsafe.assert_called_once_with(
            bot._enqueue,
            bot._queue,
            bytes(AUDIO_BYTES_PER_FRAME),
        )

    def test_bots_track_activity_independently(self):
        sound = self.make_bot('SoundBot')
        dj = self.make_bot('DJ')
        self.connect_fake_session(sound)
        self.connect_fake_session(dj)

        with mock.patch('meet_bot.time.monotonic', return_value=1000.0):
            sound.feed(bytes(AUDIO_BYTES_PER_FRAME))

        self.assertEqual(sound._last_audio_at, 1000.0)
        self.assertEqual(dj._last_audio_at, 100.0)

    def test_feed_does_not_refresh_disconnected_bot(self):
        bot = self.make_bot()
        bot._loop = mock.Mock()
        bot._queue = mock.Mock()
        bot.connected = False
        bot._last_audio_at = None

        with mock.patch('meet_bot.time.monotonic', return_value=500.0):
            bot.feed(bytes(AUDIO_BYTES_PER_FRAME))

        self.assertIsNone(bot._last_audio_at)
        bot._loop.call_soon_threadsafe.assert_not_called()

    def test_invalid_timeout_uses_one_hour_default(self):
        with mock.patch.dict(os.environ, {
            'MEET_URL': 'https://schnackn.example',
            'MEET_IDLE_TIMEOUT_SECONDS': 'invalid',
        }, clear=True):
            bot = MeetBot(mock.Mock(), 'SoundBot')

        self.assertEqual(bot.idle_timeout_seconds, 3600.0)


class ArtworkVideoTests(unittest.TestCase):
    def setUp(self):
        self.art = None
        with mock.patch.dict(os.environ, {'MEET_URL': 'https://schnackn.example'}, clear=True):
            self.bot = MeetBot(mock.Mock(), 'SoundBot', artwork=lambda: self.art)
        self.source = mock.Mock()
        self.track = mock.Mock()
        self.video = ArtworkVideo(self.bot, self.source, self.track, make_frame=lambda rgba: ('frame', rgba))
        render = mock.patch.object(ArtworkVideo, '_render', side_effect=lambda art: art.title.encode())
        self.render = render.start()
        self.addCleanup(render.stop)

    def step(self, now):
        asyncio.run(self.video.step(now))

    def test_stays_muted_without_artwork(self):
        self.step(0.0)

        self.track.unmute.assert_not_called()
        self.source.capture_frame.assert_not_called()

    def test_unmutes_and_renders_once_per_artwork(self):
        self.art = Artwork('a', 'Song', None, lambda: None)

        self.step(0.0)
        self.step(0.2)

        self.track.unmute.assert_called_once()
        self.render.assert_called_once()
        self.assertEqual(self.source.capture_frame.call_args_list, [mock.call(('frame', b'Song'))] * 2)

    def test_rerenders_when_key_changes(self):
        self.art = Artwork('a', 'One', None, lambda: None)
        self.step(0.0)
        self.art = Artwork('b', 'Two', None, lambda: None)
        self.step(0.2)

        self.assertEqual(self.render.call_count, 2)
        self.source.capture_frame.assert_called_with(('frame', b'Two'))

    def test_keeps_last_picture_briefly_then_mutes(self):
        self.art = Artwork('a', 'Song', None, lambda: None)
        self.step(0.0)
        self.art = None

        self.step(1.0)
        self.step(1.0 + VIDEO_OFF_DELAY_SECONDS - 0.01)
        self.track.mute.assert_not_called()
        self.assertEqual(self.source.capture_frame.call_count, 3)

        self.step(1.0 + VIDEO_OFF_DELAY_SECONDS)
        self.track.mute.assert_called_once()
        self.assertEqual(self.source.capture_frame.call_count, 3)

    def test_provider_errors_are_treated_as_no_artwork(self):
        self.bot.artwork = mock.Mock(side_effect=RuntimeError('boom'))

        self.step(0.0)

        self.track.unmute.assert_not_called()

    def test_video_can_be_disabled(self):
        with mock.patch.dict(os.environ, {'MEET_URL': 'https://x', 'MEET_VIDEO': '0'}, clear=True):
            bot = MeetBot(mock.Mock(), 'DJ', artwork=lambda: None)

        self.assertIsNone(bot.artwork)


if __name__ == '__main__':
    unittest.main()
