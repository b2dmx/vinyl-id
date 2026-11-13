"""Keeps the record's path through Music Assistant alive, and picks direct or in-sync sound.

The app hears the record through a silent speaker (the listener) that is grouped onto the
line-in of another player (for example an Apollo CAST). That grouping is live playback,
not a setting: any restart drops it. This puts it back.

When the line-in player can also play its input directly (a "monitor" select with a direct
and an in-sync option), direct is used while only the listener is grouped onto it, and
in-sync as soon as other speakers join, so every room plays together.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import time

from homeassistant.components.media_player import (
    ATTR_GROUP_MEMBERS,
    ATTR_INPUT_SOURCE,
    DOMAIN as MP_DOMAIN,
    MediaPlayerState,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_OPTION, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

_LOGGER = logging.getLogger(__name__)

CHECK_EVERY = timedelta(seconds=60)
RETRY_AFTER = 45.0          # seconds between two attempts to reconnect
GONE = (STATE_UNAVAILABLE, STATE_UNKNOWN)


class Router:
    def __init__(self, hass: HomeAssistant, source_player: str, source_name: str, listener: str,
                 monitor: str | None) -> None:
        self.hass = hass
        self.player = source_player
        self.source = source_name
        self.listener = listener
        self.monitor = monitor
        self._last_try = 0.0
        self._busy = False

    @callback
    def start(self) -> list[CALLBACK_TYPE]:
        watch = [e for e in (self.player, self.listener, self.monitor) if e]
        return [
            async_track_state_change_event(self.hass, watch, self._changed),
            async_track_time_interval(self.hass, self._tick, CHECK_EVERY),
        ]

    async def _changed(self, _event: Event) -> None:
        await self.check()

    async def _tick(self, _now) -> None:
        await self.check()

    def _state(self, entity_id: str) -> State | None:
        state = self.hass.states.get(entity_id)
        return None if state is None or state.state in GONE else state

    async def check(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self._check()
        except Exception:  # noqa: BLE001  a failed call must not stop later checks
            _LOGGER.exception("Could not look after the record's path")
        finally:
            self._busy = False

    async def _check(self) -> None:
        player, listener = self._state(self.player), self._state(self.listener)
        if player is None or listener is None:
            return                                   # a device is away: wait for it
        on_line_in = player.attributes.get(ATTR_INPUT_SOURCE) == self.source
        playing = player.state == MediaPlayerState.PLAYING
        members = player.attributes.get(ATTR_GROUP_MEMBERS) or []

        if not (on_line_in and playing and self.listener in members):
            if playing and not on_line_in:
                return                               # busy with other music: never interrupt it
            if time.monotonic() - self._last_try < RETRY_AFTER:
                return
            self._last_try = time.monotonic()
            _LOGGER.info("Reconnecting the record: %s -> %s", self.source, self.listener)
            if not (on_line_in and playing):
                await self._call(MP_DOMAIN, "select_source", self.player, source=self.source)
            await self._call(MP_DOMAIN, "join", self.player, group_members=[self.listener])
            return

        await self._set_monitor(others=[m for m in members if m not in (self.player, self.listener)])

    async def _set_monitor(self, others: list[str]) -> None:
        """Direct while only the listener is on the line-in; in sync once other speakers join."""
        select = self._state(self.monitor) if self.monitor else None
        options = (select.attributes.get("options") or []) if select else []
        if len(options) < 2:
            return
        want = options[1] if others else options[0]   # by convention: direct first, in-sync second
        if select.state != want:
            _LOGGER.info("Line-in sound: %s (%d other speakers)", want, len(others))
            await self._call("select", "select_option", self.monitor, **{ATTR_OPTION: want})

    async def _call(self, domain: str, service: str, entity_id: str, **data) -> None:
        await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
