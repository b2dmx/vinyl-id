"""Audio input. ffmpeg turns any source into 16 kHz mono 16-bit PCM blocks.

The source is the silent "vinyl" speaker the Sendspin or AirPlay receiver plays
into, or anything else ffmpeg can read (stream URL, device, file).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator

import numpy as np

from .config import BLOCK_SECONDS, SAMPLE_RATE

log = logging.getLogger(__name__)
BLOCK_BYTES = int(SAMPLE_RATE * BLOCK_SECONDS) * 2


async def _log_stderr(stream: asyncio.StreamReader) -> None:
    """Keep ffmpeg's error pipe empty; a full pipe would freeze ffmpeg."""
    async for line in stream:
        if text := line.decode(errors="ignore").strip():
            log.warning("ffmpeg: %s", text)


def ffmpeg_input(source: str, input_format: str = "") -> list[str]:
    """The ffmpeg command up to and including the input."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if input_format:
        cmd += ["-f", input_format]
    if os.path.isfile(source):  # test files play in real time
        cmd += ["-re"]
    if source.startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    return [*cmd, "-i", source]


async def ffmpeg_blocks(source: str, input_format: str = "") -> AsyncIterator[np.ndarray | None]:
    """Yield int16 mono blocks forever, restarting ffmpeg if the source drops (None marks a drop)."""
    cmd = [*ffmpeg_input(source, input_format), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]

    while True:
        log.info("Opening audio input: %s", source)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        assert proc.stdout is not None and proc.stderr is not None
        errors = asyncio.create_task(_log_stderr(proc.stderr))
        try:
            while True:
                data = await proc.stdout.readexactly(BLOCK_BYTES)
                yield np.frombuffer(data, dtype=np.int16)
        except asyncio.IncompleteReadError:
            log.warning("Audio input ended; retrying in 5 s")
            yield None      # nothing is playing while there is no input
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            errors.cancel()
        await asyncio.sleep(5)


def level_db(block: np.ndarray) -> float:
    """RMS level in dBFS."""
    x = block.astype(np.float32)
    rms = float(np.sqrt(np.dot(x, x) / max(len(x), 1)))
    return 20 * np.log10(max(rms, 1.0) / 32768.0)
