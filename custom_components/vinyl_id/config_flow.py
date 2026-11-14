"""Set-up in two short steps: where the app is and what you listen on, then the line-in."""

from __future__ import annotations

import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er, selector

from . import set_listener_hidden
from .const import (
    CONF_LISTENER,
    CONF_MONITOR,
    CONF_SOURCE_NAME,
    CONF_SOURCE_PLAYER,
    CONF_SPEAKER,
    DEFAULT_URL,
    DOMAIN,
)
from .coordinator import fetch_status

ADVANCED = "advanced"
PLAYER = selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))
SWITCH = selector.EntitySelector(selector.EntitySelectorConfig(domain="select"))
LINE_IN_NAMES = (r"sendspin source", r"line.?in|aux|analog")   # the usual name first, then any that says line-in


def _field(key: str, value: Any, required: bool = False) -> vol.Marker:
    """An optional (or required) field, shown filled in with `value` when there is one."""
    kind = vol.Required if required else vol.Optional
    return kind(key, description={"suggested_value": value}) if value else kind(key)


def _sources(hass: HomeAssistant, player: str | None) -> list[str]:
    state = hass.states.get(player) if player else None
    return list(state.attributes.get("source_list") or []) if state else []


def _line_in(sources: list[str]) -> str | None:
    """The source that is the line-in, when its name says so."""
    for wanted in LINE_IN_NAMES:
        for source in sources:
            if re.search(wanted, source.lower()):
                return source
    return None


def _guess_player(hass: HomeAssistant) -> str | None:
    """A player that has a line-in among its sources."""
    for wanted in LINE_IN_NAMES:
        for state in hass.states.async_all("media_player"):
            if any(re.search(wanted, s.lower()) for s in state.attributes.get("source_list") or []):
                return state.entity_id
    return None


def _find_listener(hass: HomeAssistant, name: str = "") -> str | None:
    """The app's silent speaker: by the name the app gives, else by "Vinyl ID..."."""
    for e in er.async_get(hass).entities.values():
        if e.domain != "media_player" or e.platform == DOMAIN:
            continue
        state = hass.states.get(e.entity_id)      # entities named after their device have no name of their own
        have = (e.name or e.original_name or (state.name if state else "")).lower()
        if have == name.lower() if name else have.startswith("vinyl id"):
            return e.entity_id
    return None


def _guess_switch(hass: HomeAssistant) -> str | None:
    return next((s.entity_id for s in hass.states.async_all("select")
                 if s.entity_id.endswith("_line_in_monitor")), None)


class _LineInStep:
    """The second step, the same when setting up and when changing things later."""

    hass: HomeAssistant
    _picked: dict[str, Any]         # what the first step gave
    _listener_name: str = ""        # what the app calls its silent speaker

    def _line_in_form(self, step_id: str, old: dict[str, Any]):
        player = self._picked[CONF_SOURCE_PLAYER]
        sources = _sources(self.hass, player)
        source = selector.SelectSelector(selector.SelectSelectorConfig(
            options=sources, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN)
        ) if sources else selector.TextSelector()
        same = old.get(CONF_SOURCE_PLAYER) == player
        schema = vol.Schema({
            _field(CONF_SOURCE_NAME, (old.get(CONF_SOURCE_NAME) if same else None) or _line_in(sources),
                   required=True): source,
            _field(CONF_LISTENER, old.get(CONF_LISTENER) or _find_listener(self.hass, self._listener_name),
                   required=True): PLAYER,
            vol.Required(ADVANCED): section(
                vol.Schema({_field(CONF_MONITOR, old.get(CONF_MONITOR) or _guess_switch(self.hass)): SWITCH}),
                {"collapsed": True}),
        })
        return self.async_show_form(step_id=step_id, data_schema=schema)  # type: ignore[attr-defined]

    def _options(self, line_in: dict[str, Any] | None) -> dict[str, Any]:
        opts = {k: v for k, v in self._picked.items() if v}
        if line_in:
            opts |= {CONF_SOURCE_NAME: line_in[CONF_SOURCE_NAME], CONF_LISTENER: line_in[CONF_LISTENER]}
            if switch := (line_in.get(ADVANCED) or {}).get(CONF_MONITOR):
                opts[CONF_MONITOR] = switch
        elif listener := _find_listener(self.hass, self._listener_name):
            opts[CONF_LISTENER] = listener         # no line-in to look after: only kept off dashboards
        return opts


class VinylConfigFlow(_LineInStep, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url = ""
        self._picked = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            try:
                status = await fetch_status(self.hass, url)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                self._url, self._listener_name = url, str(status.get("listener") or "")
                self._picked = {k: user_input.get(k) for k in (CONF_SPEAKER, CONF_SOURCE_PLAYER)}
                if self._picked[CONF_SOURCE_PLAYER]:
                    return await self.async_step_line_in()
                return self.async_create_entry(title="Vinyl ID", data={CONF_URL: url}, options=self._options(None))
        given = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_URL, default=given.get(CONF_URL, DEFAULT_URL)): str,
            _field(CONF_SPEAKER, given.get(CONF_SPEAKER)): PLAYER,
            _field(CONF_SOURCE_PLAYER, given.get(CONF_SOURCE_PLAYER) or _guess_player(self.hass)): PLAYER,
        })
        return self.async_show_form(step_id="user", errors=errors, data_schema=schema)

    async def async_step_line_in(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self._line_in_form("line_in", {})
        return self.async_create_entry(title="Vinyl ID", data={CONF_URL: self._url}, options=self._options(user_input))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The app moved (another machine, another port)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            try:
                await fetch_status(self.hass, url)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                # the entry's update listener reloads it
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_URL: url})
                return self.async_abort(reason="reconfigure_successful")
        schema = vol.Schema({vol.Required(CONF_URL, default=entry.data[CONF_URL]): str})
        return self.async_show_form(
            step_id="reconfigure", errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VinylOptionsFlow()


class VinylOptionsFlow(_LineInStep, OptionsFlow):
    def __init__(self) -> None:
        self._picked = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        old = self.config_entry.options
        if user_input is not None:
            self._picked = {k: user_input.get(k) for k in (CONF_SPEAKER, CONF_SOURCE_PLAYER)}
            if self._picked[CONF_SOURCE_PLAYER]:
                return await self.async_step_line_in()
            return self._save(None)
        schema = vol.Schema({
            _field(CONF_SPEAKER, old.get(CONF_SPEAKER)): PLAYER,
            # what is still empty is guessed again: the line-in player may not have existed at set-up
            _field(CONF_SOURCE_PLAYER, old.get(CONF_SOURCE_PLAYER) or _guess_player(self.hass)): PLAYER,
        })
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_line_in(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self._line_in_form("line_in", dict(self.config_entry.options))
        return self._save(user_input)

    def _save(self, line_in: dict[str, Any] | None) -> ConfigFlowResult:
        new, old = self._options(line_in), self.config_entry.options.get(CONF_LISTENER)
        if not line_in and old:
            new[CONF_LISTENER] = old                      # still the app's speaker, still kept off dashboards
        if old != new.get(CONF_LISTENER):                 # no longer the listener: show it again
            set_listener_hidden(self.hass, old, False)
        return self.async_create_entry(data=new)
