"""Settings, read from environment variables (set them in docker-compose.yml)."""

from __future__ import annotations

import os
from dataclasses import dataclass

APP_NAME = "Vinyl ID"
VERSION = "0.5.2"
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
    start_seconds: float = _f("START_SECONDS", 2)        # sound this long = record started
    stop_seconds: float = _f("STOP_SECONDS", 45)         # quiet this long = record stopped (covers a side flip)
    gap_seconds: float = _f("GAP_SECONDS", 2)            # dip this long between songs = track change
    gap_drop_db: float = _f("GAP_DROP_DB", 18)           # how far below the song's level a gap must dip

    # Recognition
    sample_seconds: float = _f("SAMPLE_SECONDS", 12)
    settle_seconds: float = _f("SETTLE_SECONDS", 6)      # wait after start / track change before sampling
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
    web_port: int = int(_f("WEB_PORT", 8095))
    public_url: str = _s("PUBLIC_URL", "http://localhost:8095")  # how HA reaches this service

    edit_from: str = _s("EDIT_FROM")  # comma-separated IPs allowed to edit; blank = anyone on the LAN

    log_level: str = _s("LOG_LEVEL", "INFO")


SAMPLE_RATE = 16000  # Shazam fingerprints at 16 kHz mono
BLOCK_SECONDS = 0.25
