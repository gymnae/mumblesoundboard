"""Pure PCM helpers and offline diagnostics for the soundboard audio path."""

from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
from dataclasses import asdict, dataclass
from queue import Empty, Full
from typing import Iterable, Sequence

SAMPLE_RATE = 48_000
CHANNELS = 1
SAMPLE_WIDTH = 2
SAMPLES_PER_FRAME = 960
FRAME_DURATION_SECONDS = SAMPLES_PER_FRAME / SAMPLE_RATE
BYTES_PER_FRAME = SAMPLES_PER_FRAME * SAMPLE_WIDTH
PCM_BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
INT16_MIN = -32_768
INT16_MAX = 32_767


def unpack_pcm(chunk: bytes) -> tuple[int, ...]:
    """Decode one or more little-endian signed 16-bit samples."""
    if len(chunk) % SAMPLE_WIDTH:
        raise ValueError("s16le PCM length must be divisible by two")
    return struct.unpack(f"<{len(chunk) // SAMPLE_WIDTH}h", chunk)


def pack_pcm(samples: Iterable[float]) -> bytes:
    """Round, saturate, and encode samples as little-endian signed 16-bit PCM."""
    values = [max(INT16_MIN, min(INT16_MAX, round(sample))) for sample in samples]
    return struct.pack(f"<{len(values)}h", *values)


def mix_pcm(*chunks: bytes | None, prevent_clipping: bool = True) -> bytes | None:
    """Mix equally sized PCM chunks and apply transparent peak protection.

    Peak protection changes no sample while the sum fits int16. If it would clip,
    one common gain is applied to the whole frame, preserving the mix balance and
    avoiding hard-clipping distortion.
    """
    present = [chunk for chunk in chunks if chunk]
    if not present:
        return None
    expected = len(present[0])
    if any(len(chunk) != expected for chunk in present):
        raise ValueError("PCM chunks must have equal lengths")
    if len(present) == 1:
        return present[0]

    tracks = [unpack_pcm(chunk) for chunk in present]
    summed = [sum(samples) for samples in zip(*tracks)]
    peak = max((abs(sample) for sample in summed), default=0)
    gain = (INT16_MAX / peak) if prevent_clipping and peak > INT16_MAX else 1.0
    return pack_pcm(sample * gain for sample in summed)


def enqueue_latest(target_queue, item) -> bool:
    """Enqueue an item, replacing the oldest when full; return whether one was dropped."""
    try:
        target_queue.put_nowait(item)
        return False
    except Full:
        try:
            target_queue.get_nowait()
        except Empty:
            pass
        target_queue.put_nowait(item)
        return True


@dataclass(frozen=True)
class PcmMetrics:
    duration_seconds: float
    samples: int
    peak: int
    peak_dbfs: float | None
    rms: float
    rms_dbfs: float | None
    clipped_samples: int
    clipped_percent: float
    frames: int
    partial_frame_bytes: int
    effective_pcm_bitrate: int


def _dbfs(value: float) -> float | None:
    return 20 * math.log10(value / INT16_MAX) if value > 0 else None


def analyze_pcm(pcm: bytes) -> PcmMetrics:
    """Measure decoded s16le mono 48 kHz PCM without changing it."""
    samples = unpack_pcm(pcm)
    count = len(samples)
    peak = max((abs(sample) for sample in samples), default=0)
    rms = math.sqrt(sum(sample * sample for sample in samples) / count) if count else 0.0
    clipped = sum(sample in (INT16_MIN, INT16_MAX) for sample in samples)
    return PcmMetrics(
        duration_seconds=count / SAMPLE_RATE,
        samples=count,
        peak=peak,
        peak_dbfs=_dbfs(peak),
        rms=rms,
        rms_dbfs=_dbfs(rms),
        clipped_samples=clipped,
        clipped_percent=(100 * clipped / count) if count else 0.0,
        frames=len(pcm) // BYTES_PER_FRAME,
        partial_frame_bytes=len(pcm) % BYTES_PER_FRAME,
        effective_pcm_bitrate=PCM_BYTES_PER_SECOND * 8,
    )


def generate_sine(seconds: float = 5.0, frequency: float = 997.0, level_dbfs: float = -12.0) -> bytes:
    """Generate a deterministic diagnostic sine wave in the internal format."""
    amplitude = INT16_MAX * 10 ** (level_dbfs / 20)
    count = round(seconds * SAMPLE_RATE)
    return pack_pcm(amplitude * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE) for i in range(count))


def decode_file(path: str, seconds: float | None = None) -> bytes:
    """Decode an audio file through ffmpeg exactly as the application does."""
    command = ["ffmpeg", "-v", "error", "-i", path]
    if seconds is not None:
        command.extend(["-t", str(seconds)])
    command.extend(["-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-"])
    return subprocess.check_output(command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the soundboard's pre-Opus PCM quality")
    parser.add_argument("path", nargs="?", help="audio file to decode; omitted generates a reference sine")
    parser.add_argument("--seconds", type=float, default=5.0, help="seconds to decode or generate")
    parser.add_argument("--frequency", type=float, default=997.0, help="generated sine frequency")
    parser.add_argument("--level-dbfs", type=float, default=-12.0, help="generated sine peak level")
    args = parser.parse_args(argv)
    pcm = decode_file(args.path, args.seconds) if args.path else generate_sine(args.seconds, args.frequency, args.level_dbfs)
    print(json.dumps(asdict(analyze_pcm(pcm)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
