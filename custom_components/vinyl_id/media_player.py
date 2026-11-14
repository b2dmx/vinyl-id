"""The Record Player: what the record is playing, with the chosen speaker's volume and power."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DOMAIN as MP_DOMAIN,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature as F,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import VinylConfigEntry
from .const import CONF_SPEAKER, CONF_SPEAKER_SOURCE, DOMAIN
from .coordinator import VinylCoordinator

_LOGGER = logging.getLogger(__name__)

# What the Record Player can pass on to the speaker, when the speaker can do it
PASSED_ON = F.VOLUME_SET | F.VOLUME_STEP | F.VOLUME_MUTE | F.TURN_ON | F.TURN_OFF


async def async_setup_entry(hass: HomeAssistant, entry: VinylConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([RecordPlayer(entry.runtime_data, entry)])


class RecordPlayer(CoordinatorEntity[VinylCoordinator], MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None                       # the device's name is the entity's name
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_icon = "mdi:album"

    def __init__(self, coordinator: VinylCoordinator, entry: VinylConfigEntry) -> None:
        super().__init__(coordinator)
        self._speaker: str | None = entry.options.get(CONF_SPEAKER)
        self._speaker_source: str | None = entry.options.get(CONF_SPEAKER_SOURCE)
        self._was_playing = bool((coordinator.data or {}).get("playing"))
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Record Player", manufacturer="Goobis",
            model="Vinyl ID", configuration_url=coordinator.url)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._speaker:
            self.async_on_remove(async_track_state_change_event(
                self.hass, [self._speaker], self._speaker_changed))

    @callback
    def _speaker_changed(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        playing = bool((self.coordinator.data or {}).get("playing"))
        if playing and not self._was_playing and self._speaker_source:
            self.hass.async_create_task(self._to_record_input())      # the needle dropped
        self._was_playing = playing
        super()._handle_coordinator_update()

    async def _to_record_input(self) -> None:
        """Put the speaker on the input the record comes in on, so nobody has to."""
        state = self._speaker_state
        if state is None or state.attributes.get("source") == self._speaker_source:
            return
        try:
            await self.hass.services.async_call(
                MP_DOMAIN, "select_source", {ATTR_ENTITY_ID: self._speaker, "source": self._speaker_source},
                blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch %s to %s: %s", self._speaker, self._speaker_source, err)

    # --- the record -----------------------------------------------------------
    @property
    def _now(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return (data.get("now") or {}) if data.get("playing") else {}

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.PLAYING if (self.coordinator.data or {}).get("playing") else MediaPlayerState.IDLE

    @property
    def media_title(self) -> str | None:
        return self._now.get("title")

    @property
    def media_artist(self) -> str | None:
        return self._now.get("artist") or None

    @property
    def media_album_name(self) -> str | None:
        return self._now.get("album") or None

    @property
    def media_image_url(self) -> str | None:
        """Home Assistant fetches the cover itself, so from the address it reaches the app on
        (the address the app gives is the one for other devices, and may not work from here)."""
        url = self._now.get("art_url")
        if not url:
            return None
        parts = urlsplit(url)
        path = parts.path[max(parts.path.find("/api/"), 0):]     # without any prefix of the app's own address
        return f"{self.coordinator.url.rstrip('/')}{path}" + (f"?{parts.query}" if parts.query else "")

    @property
    def media_track(self) -> int | None:
        return self._now.get("track_number")

    @property
    def media_duration(self) -> int | None:
        length = self._now.get("track_length")
        return round(length) if length else None

    # Home Assistant works the live position out as "position + time since updated_at".
    # Saying "0 at the moment the track started" never changes while a track plays, so the
    # state is only written when something really changes.
    @property
    def media_position(self) -> int | None:
        return 0 if self.media_position_updated_at else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        return dt_util.parse_datetime(self._now.get("track_started_at") or "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = self._now
        attrs = {"side": now.get("side"), "side_tracks": now.get("side_tracks"),
                 "album_tracks": now.get("album_tracks"), "identified_by": now.get("source"),
                 "speaker": self._speaker}
        return {k: v for k, v in attrs.items() if v is not None}

    # --- the speaker ------------------------------------------------------------
    @property
    def _speaker_state(self) -> State | None:
        state = self.hass.states.get(self._speaker) if self._speaker else None
        return None if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) else state

    @property
    def supported_features(self) -> F:
        state = self._speaker_state
        return F(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)) & PASSED_ON if state else F(0)

    @property
    def volume_level(self) -> float | None:
        state = self._speaker_state
        return state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL) if state else None

    @property
    def is_volume_muted(self) -> bool | None:
        state = self._speaker_state
        return state.attributes.get(ATTR_MEDIA_VOLUME_MUTED) if state else None

    async def _speaker_call(self, service: str, **data: Any) -> None:
        await self.hass.services.async_call(
            MP_DOMAIN, service, {ATTR_ENTITY_ID: self._speaker, **data}, blocking=True, context=self._context)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._speaker_call("volume_set", volume_level=volume)

    async def async_volume_up(self) -> None:
        await self._speaker_call("volume_up")

    async def async_volume_down(self) -> None:
        await self._speaker_call("volume_down")

    async def async_mute_volume(self, mute: bool) -> None:
        await self._speaker_call("volume_mute", is_volume_muted=mute)

    async def async_turn_on(self) -> None:
        await self._speaker_call("turn_on")

    async def async_turn_off(self) -> None:
        await self._speaker_call("turn_off")
