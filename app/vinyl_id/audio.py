"""Audio input. ffmpeg turns any source into 16 kHz mono 16-bit PCM blocks.

The CAST PRO-1 tap (Sendspin listener or Music Assistant stream URL) gets
decided once the hardware is here; anything ffmpeg can read works today.
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


async def ffmpeg_blocks(source: str, input_format: str = "") -> AsyncIterator[np.ndarray]:
    """Yield int16 mono blocks forever, restarting ffmpeg if the source drops."""
    while True:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if input_format:
            cmd += ["-f", input_format]
        if os.path.isfile(source):  # test files play in real time
            cmd += ["-re"]
        if source.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        cmd += ["-i", source, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]

        log.info("Opening audio input: %s", source)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        assert proc.stdout is not None
        try:
            while True:
                data = await proc.stdout.readexactly(BLOCK_BYTES)
                yield np.frombuffer(data, dtype=np.int16)
        except asyncio.IncompleteReadError:
            err = (await proc.stderr.read()).decode(errors="ignore").strip() if proc.stderr else ""
            log.warning("Audio input ended%s; retrying in 5 s", f": {err}" if err else "")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.sleep(5)


def level_db(block: np.ndarray) -> float:
    """RMS level in dBFS."""
    rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
    return 20 * np.log10(max(rms, 1.0) / 32768.0)
