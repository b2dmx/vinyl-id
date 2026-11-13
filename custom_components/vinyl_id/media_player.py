"""The Record Player: what the record is playing, with the chosen speaker's volume and power."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import VinylConfigEntry
from .const import CONF_SPEAKER, DOMAIN
from .coordinator import VinylCoordinator

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
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Record Player", manufacturer="Goobis",
            model="Vinyl ID", configuration_url=coordinator.url)
        self._read_at: datetime = dt_util.utcnow()

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
        self._read_at = dt_util.utcnow()
        super()._handle_coordinator_update()

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
        return self._now.get("art_url") or None

    @property
    def media_track(self) -> int | None:
        return self._now.get("track_number")

    @property
    def media_duration(self) -> int | None:
        length = self._now.get("track_length")
        return round(length) if length else None

    @property
    def media_position(self) -> int | None:
        started = dt_util.parse_datetime(self._now.get("track_started_at") or "")
        if started is None:
            return None
        pos = max(0.0, (self._read_at - started).total_seconds())
        return round(min(pos, self.media_duration or pos))

    @property
    def media_position_updated_at(self) -> datetime | None:
        return self._read_at if self.media_position is not None else None

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
