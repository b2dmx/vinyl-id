"""The audio input itself."""

import asyncio
import sys

from vinyl_id import audio


def test_a_source_that_hangs_is_reported_and_reopened(monkeypatch):
    script = f"import sys, time; sys.stdout.buffer.write(bytes({audio.BLOCK_BYTES})); sys.stdout.flush(); time.sleep(30)"
    monkeypatch.setattr(audio, "ffmpeg_input", lambda *_a: [sys.executable, "-c", script])
    monkeypatch.setattr(audio, "STALL_SECONDS", 1)

    async def go():
        blocks = audio.ffmpeg_blocks("x")
        first, second = await blocks.__anext__(), await blocks.__anext__()
        await blocks.aclose()
        return first, second

    first, second = asyncio.run(go())
    assert first is not None and len(first) == audio.BLOCK_BYTES // 2
    assert second is None                              # "nothing is playing" reaches the detector
