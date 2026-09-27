import math
import queue
import unittest

from audio_quality import (
    BYTES_PER_FRAME,
    FRAME_DURATION_SECONDS,
    PCM_BYTES_PER_SECOND,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    analyze_pcm,
    enqueue_latest,
    generate_sine,
    mix_pcm,
    pack_pcm,
    unpack_pcm,
)


class PcmHelpersTests(unittest.TestCase):
    def test_frame_contract_is_twenty_ms_mono_s16le(self):
        self.assertEqual(SAMPLE_RATE, 48_000)
        self.assertEqual(SAMPLES_PER_FRAME, 960)
        self.assertEqual(BYTES_PER_FRAME, 1920)
        self.assertEqual(FRAME_DURATION_SECONDS, 0.02)

    def test_pack_saturates_instead_of_wrapping(self):
        self.assertEqual(unpack_pcm(pack_pcm([-40_000, 0, 40_000])), (-32_768, 0, 32_767))

    def test_single_track_is_bit_exact(self):
        chunk = pack_pcm([-1234, 0, 5678])
        self.assertIs(mix_pcm(chunk), chunk)

    def test_mix_preserves_sum_when_headroom_is_available(self):
        left = pack_pcm([1000, -2000, 3000])
        right = pack_pcm([4000, 1000, -1000])
        self.assertEqual(unpack_pcm(mix_pcm(left, right)), (5000, -1000, 2000))

    def test_mix_peak_protection_avoids_hard_clipping(self):
        loud = pack_pcm([30_000, -30_000, 15_000])
        mixed = unpack_pcm(mix_pcm(loud, loud))
        self.assertEqual(mixed[0], 32_767)
        self.assertEqual(mixed[1], -32_767)
        self.assertEqual(mixed[2], 16_384)
        self.assertNotIn(-32_768, mixed)

    def test_mix_rejects_different_frame_lengths(self):
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            mix_pcm(pack_pcm([1]), pack_pcm([1, 2]))

    def test_enqueue_latest_drops_oldest_on_overflow(self):
        target = queue.Queue(maxsize=2)
        self.assertFalse(enqueue_latest(target, b"oldest"))
        self.assertFalse(enqueue_latest(target, b"middle"))
        self.assertTrue(enqueue_latest(target, b"latest"))
        self.assertEqual(target.get_nowait(), b"middle")
        self.assertEqual(target.get_nowait(), b"latest")


class DiagnosticsTests(unittest.TestCase):
    def test_sine_metrics_are_predictable(self):
        pcm = generate_sine(seconds=1, frequency=1000, level_dbfs=-12)
        metrics = analyze_pcm(pcm)
        self.assertEqual(metrics.samples, SAMPLE_RATE)
        self.assertEqual(metrics.frames, 50)
        self.assertEqual(metrics.partial_frame_bytes, 0)
        self.assertEqual(metrics.clipped_samples, 0)
        self.assertEqual(metrics.effective_pcm_bitrate, PCM_BYTES_PER_SECOND * 8)
        self.assertAlmostEqual(metrics.peak_dbfs, -12, delta=0.01)
        self.assertAlmostEqual(metrics.rms_dbfs, -12 - 20 * math.log10(math.sqrt(2)), delta=0.02)

    def test_diagnostic_reports_partial_frames_and_clipping(self):
        metrics = analyze_pcm(pack_pcm([-32_768, 32_767, 0]))
        self.assertEqual(metrics.clipped_samples, 2)
        self.assertAlmostEqual(metrics.clipped_percent, 200 / 3)
        self.assertEqual(metrics.frames, 0)
        self.assertEqual(metrics.partial_frame_bytes, 6)


if __name__ == "__main__":
    unittest.main()
