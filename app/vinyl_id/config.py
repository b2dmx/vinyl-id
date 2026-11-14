"""Settings, read from environment variables (set them in docker-compose.yml)."""

from __future__ import annotations

import os
from dataclasses import dataclass

APP_NAME = "Vinyl ID"
VERSION = "0.9.1"
SIGNATURE = f"{APP_NAME} v{VERSION} by Goobis"
AI_NOTE = "AI was used to help develop this application"


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # Audio input: anything ffmpeg can open (stream URL, ALSA device, file).
    audio_input: str = _s("AUDIO_INPUT")
    audio_input_format: str = _s("AUDIO_INPUT_FORMAT")  # e.g. "alsa"; blank = auto

    # Signal detection (dBFS). Tune with the "Input Level" sensor in HA.
    signal_threshold_db: float = _f("SIGNAL_THRESHOLD_DB", -50)
    start_margin_db: float = _f("START_MARGIN_DB", 12)   # a record starts only with sound this far above the threshold
    start_seconds: float = _f("START_SECONDS", 2)        # sound this long = record started
    stop_seconds: float = _f("STOP_SECONDS", 45)         # quiet this long = record stopped (covers a side flip)
    gap_seconds: float = _f("GAP_SECONDS", 2)            # dip this long between songs = track change
    gap_ceiling_db: float = _f("GAP_CEILING_DB", 6)      # a gap (or the side's end) stays within this of the threshold

    # Recognition
    sample_seconds: float = _f("SAMPLE_SECONDS", 12)
    quick_sample_seconds: float = _f("QUICK_SAMPLE_SECONDS", 5)   # a new song is first tried on this much audio
    settle_seconds: float = _f("SETTLE_SECONDS", 3)      # wait after start / track change before sampling
    recheck_seconds: float = _f("RECHECK_SECONDS", 150)  # catches gapless track changes
    retry_seconds: float = _f("RETRY_SECONDS", 30)       # when a sample wasn't recognized

    # MQTT
    mqtt_host: str = _s("MQTT_HOST", "homeassistant.local")
    mqtt_port: int = int(_f("MQTT_PORT", 1883))
    mqtt_user: str = _s("MQTT_USER")
    mqtt_password: str = _s("MQTT_PASSWORD")
    base_topic: str = _s("MQTT_BASE_TOPIC", "vinyl_id")
    discovery_prefix: str = _s("MQTT_DISCOVERY_PREFIX", "homeassistant")

    # Learning: save fingerprints so replays are identified locally.
    # Off = always ask Shazam (covers and your edits are still saved).
    learn: bool = _s("LEARN", "true").lower() in ("1", "true", "yes", "on")

    # Library + web UI
    data_dir: str = _s("DATA_DIR", "/data")
    max_library_mb: float = _f("MAX_LIBRARY_MB", 0)      # stop learning at this size on disk; 0 = no limit
    web_port: int = int(_f("WEB_PORT", 8780))            # not 8095: that is Music Assistant's
    public_url: str = _s("PUBLIC_URL", "http://localhost:8780")  # how HA reaches this service

    receiver_name: str = _s("RECEIVER_NAME", "Vinyl ID Listener")  # the silent speaker's name in Music Assistant

    edit_from: str = _s("EDIT_FROM")  # comma-separated IPs allowed to edit; blank = anyone on the LAN

    log_level: str = _s("LOG_LEVEL", "INFO")


SAMPLE_RATE = 16000  # Shazam fingerprints at 16 kHz mono
BLOCK_SECONDS = 0.25
