"""Keeps the record's path through Music Assistant alive, and picks direct or in-sync sound.

The app hears the record through a silent speaker (the listener) that is grouped onto the
line-in of another player (for example an Apollo CAST). That grouping is live playback,
not a setting: a restart of the app, Music Assistant or the player drops it. This puts it
back, as long as the line-in player is idle (it is never taken away from other music).

When the line-in player can also play its input directly (a "monitor" select with a direct
and an in-sync option), direct is used while only the listener is grouped onto it, and
in-sync as soon as other speakers join, so every room plays together.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

CHECK_EVERY = timedelta(seconds=60)
RETRY_AFTER = 45.0          # seconds between two attempts to reconnect
RETRY_AT_MOST = 600.0       # ... growing to this while the attempts keep failing
IDLE_FOR = 10.0             # idle this long before reconnecting: between two songs a player is idle for a moment
GONE = (STATE_UNAVAILABLE, STATE_UNKNOWN)


class Router:
    def __init__(self, hass: HomeAssistant, source_player: str, source_name: str, listener: str,
                 monitor: str | None, hears_nothing: Callable[[], bool] = lambda: False) -> None:
        self.hass = hass
        self.player = source_player
        self.source = source_name
        self.listener = listener
        self.monitor = monitor
        self.hears_nothing = hears_nothing       # the app says no sound reaches it at all
        self._last_try = 0.0
        self._retry = RETRY_AFTER
        self._busy = False
        self._again = False                      # something changed while a check was running
        self._later: CALLBACK_TYPE | None = None
        self._monitor_failed = 0.0               # when switching direct/in-sync last failed

    @callback
    def start(self) -> list[CALLBACK_TYPE]:
        watch = [e for e in (self.player, self.listener, self.monitor) if e]
        return [
            async_track_state_change_event(self.hass, watch, self._changed),
            async_track_time_interval(self.hass, self._tick, CHECK_EVERY),
            self._cancel_later,
        ]

    @callback
    def _cancel_later(self) -> None:
        if self._later:
            self._later()
            self._later = None

    @callback
    def _check_in(self, seconds: float) -> None:
        async def run(_now) -> None:
            self._later = None
            await self.check()

        self._cancel_later()
        self._later = async_call_later(self.hass, seconds, run)

    async def _changed(self, _event: Event) -> None:
        await self.check()

    async def _tick(self, _now) -> None:
        await self.check()

    def _state(self, entity_id: str) -> State | None:
        state = self.hass.states.get(entity_id)
        return None if state is None or state.state in GONE else state

    async def check(self) -> None:
        if self._busy:
            self._again = True                   # look again once the running check is done
            return
        self._busy = True
        try:
            for _ in range(3):
                self._again = False
                await self._check()
                if not self._again:
                    break
        except (HomeAssistantError, TimeoutError) as err:
            self._retry = min(self._retry * 2, RETRY_AT_MOST)
            _LOGGER.warning("Could not reconnect the record (next try in %d s): %s", self._retry, str(err) or type(err).__name__)
        except Exception:  # noqa: BLE001  a failed call must not stop later checks
            self._retry = min(self._retry * 2, RETRY_AT_MOST)
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
            if player.state != MediaPlayerState.IDLE and not (playing and on_line_in):
                return                               # other music, paused, or switched off: leave it alone
            if player.state == MediaPlayerState.IDLE:
                idle_for = (dt_util.utcnow() - player.last_changed).total_seconds()
                if idle_for < IDLE_FOR:
                    self._check_in(IDLE_FOR - idle_for + 1)
                    return
            if time.monotonic() - self._last_try < self._retry:
                return
            self._last_try = time.monotonic()
            _LOGGER.info("Reconnecting the record: %s -> %s", self.source, self.listener)
            if not (on_line_in and playing):
                await self._call(MP_DOMAIN, "select_source", self.player, source=self.source)
            await self._call(MP_DOMAIN, "join", self.player, group_members=[self.listener])
            return

        if self.hears_nothing():
            # Everything says "connected", yet the app gets no sound: after the app restarts, Music
            # Assistant can go on showing the old group while nothing flows. Join it again.
            if time.monotonic() - self._last_try < self._retry:
                return
            self._last_try = time.monotonic()
            _LOGGER.info("The app hears nothing although it shows as connected: joining %s again", self.listener)
            await self._call(MP_DOMAIN, "join", self.player, group_members=[self.listener])
            return
        self._retry = RETRY_AFTER                    # connected: the next drop is tried promptly again
        await self._set_monitor(others=[m for m in members if m not in (self.player, self.listener)])

    async def _set_monitor(self, others: list[str]) -> None:
        """Direct while only the listener is on the line-in; in sync once other speakers join."""
        select = self._state(self.monitor) if self.monitor else None
        options = (select.attributes.get("options") or []) if select else []
        if len(options) < 2:
            return
        want = options[1] if others else options[0]   # by convention: direct first, in-sync second
        if select.state != want and time.monotonic() - self._monitor_failed >= RETRY_AFTER:
            _LOGGER.info("Line-in sound: %s (%d other speakers)", want, len(others))
            try:
                await self._call("select", "select_option", self.monitor, **{ATTR_OPTION: want})
            except (HomeAssistantError, TimeoutError) as err:     # the record itself is connected: no back-off
                self._monitor_failed = time.monotonic()
                _LOGGER.warning("Could not switch the line-in sound to %s: %s", want, str(err) or type(err).__name__)

    async def _call(self, domain: str, service: str, entity_id: str, **data) -> None:
        async with asyncio.timeout(30):              # a call that never answers must not stop later checks
            await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)
