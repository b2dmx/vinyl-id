"""Publishes to Home Assistant via MQTT discovery, so the entities appear by themselves."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .config import APP_NAME, SIGNATURE, VERSION, Settings

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, s: Settings, on_forget: Callable[[], None]) -> None:
        self.s = s
        self.t = s.base_topic
        self.on_forget = on_forget
        self._last: dict[str, tuple[str | bytes, bool]] = {}
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="vinyl-id")
        if s.mqtt_user:
            self.c.username_pw_set(s.mqtt_user, s.mqtt_password)
        self.c.will_set(f"{self.t}/status", "offline", retain=True)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message

    def start(self) -> None:
        self.c.connect_async(self.s.mqtt_host, self.s.mqtt_port)
        self.c.loop_start()

    def stop(self) -> None:
        self.c.publish(f"{self.t}/status", "offline", retain=True)
        self.c.loop_stop()
        self.c.disconnect()

    # --- discovery -------------------------------------------------------
    def _on_connect(self, *_args) -> None:
        log.info("Connected to MQTT %s:%s", self.s.mqtt_host, self.s.mqtt_port)
        dev = {
            "identifiers": ["vinyl_id"], "name": APP_NAME, "manufacturer": "Goobis",
            "model": SIGNATURE, "sw_version": VERSION,
            "configuration_url": f"{self.s.public_url}/",
        }
        common = {"availability_topic": f"{self.t}/status", "device": dev}
        p, t = self.s.discovery_prefix, self.t
        entities = {
            f"{p}/binary_sensor/vinyl_id/playing/config": {
                "name": "Record Playing", "unique_id": "vinyl_id_playing",
                "device_class": "sound", "state_topic": f"{t}/playing", "icon": "mdi:album"},
            f"{p}/sensor/vinyl_id/now_playing/config": {
                "name": "Now Playing", "unique_id": "vinyl_id_now_playing",
                "state_topic": f"{t}/track", "value_template": "{{ value_json.title }}",
                "json_attributes_topic": f"{t}/track", "icon": "mdi:music-circle"},
            f"{p}/image/vinyl_id/album_art/config": {
                "name": "Album Art", "unique_id": "vinyl_id_album_art",
                "image_topic": f"{t}/art", "content_type": "image/jpeg"},
            f"{p}/sensor/vinyl_id/library/config": {
                "name": "Library Size", "unique_id": "vinyl_id_library",
                "state_topic": f"{t}/library", "unit_of_measurement": "songs",
                "icon": "mdi:bookshelf"},
            f"{p}/button/vinyl_id/forget/config": {
                "name": "Forget Current Song", "unique_id": "vinyl_id_forget",
                "command_topic": f"{t}/forget", "icon": "mdi:delete-outline",
                "entity_category": "config"},
            f"{p}/sensor/vinyl_id/level/config": {
                "name": "Input Level", "unique_id": "vinyl_id_level",
                "state_topic": f"{t}/level", "unit_of_measurement": "dB",
                "entity_category": "diagnostic", "icon": "mdi:waveform"},
        }
        for topic, cfg in entities.items():
            self.c.publish(topic, json.dumps({**cfg, **common}), retain=True)
        self.c.subscribe(f"{t}/forget")
        self.c.subscribe(f"{p}/status")      # HA's birth message: it restarted, so send state again
        self.c.publish(f"{t}/status", "online", retain=True)
        self._resend()

    def _resend(self) -> None:
        """Publishes made before the connection was up are dropped, so keep the last
        value of each topic and send them all again on every (re)connect."""
        for topic, (payload, retain) in list(self._last.items()):
            self.c.publish(topic, payload, retain=retain)

    def _put(self, topic: str, payload: str | bytes, retain: bool = True) -> None:
        self._last[topic] = (payload, retain)
        self.c.publish(topic, payload, retain=retain)

    def _on_message(self, _c, _u, msg) -> None:
        if msg.topic == f"{self.t}/forget":
            self.on_forget()
        elif msg.topic == f"{self.s.discovery_prefix}/status" and msg.payload == b"online":
            self._on_connect()

    # --- state -----------------------------------------------------------
    def playing(self, on: bool) -> None:
        self._put(f"{self.t}/playing", "ON" if on else "OFF")

    def track(self, info: dict | None, art: bytes | None = None) -> None:
        """info: title, artist, album, art_url, source ('local' or 'shazam')."""
        payload = {"title": "None", "artist": "", "album": "", "art_url": "", "source": ""}
        if info:
            payload |= info
            payload["recognized_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._put(f"{self.t}/track", json.dumps(payload))
        if art:
            self._put(f"{self.t}/art", art)

    def library(self, n: int) -> None:
        self._put(f"{self.t}/library", str(n))

    def level(self, db: float) -> None:
        self._put(f"{self.t}/level", f"{db:.0f}", retain=False)
