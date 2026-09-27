import unittest

from meet_bot import AUDIO_BYTES_PER_FRAME, copy_pcm_to_frame


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


if __name__ == '__main__':
    unittest.main()
