import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from meet_bot import AUDIO_BYTES_PER_FRAME, MeetBot, copy_pcm_to_frame


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


if __name__ == '__main__':
    unittest.main()
