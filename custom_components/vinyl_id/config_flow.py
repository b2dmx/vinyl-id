"""Set-up: where the Vinyl ID app is, and which speaker plays the record."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector

from . import set_listener_hidden
from .const import CONF_LISTENER, CONF_SPEAKER, DEFAULT_URL, DOMAIN
from .coordinator import fetch_status

SPEAKER = selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))
PLAYERS = {vol.Optional(CONF_SPEAKER): SPEAKER, vol.Optional(CONF_LISTENER): SPEAKER}


def _options(user_input: dict[str, Any]) -> dict[str, Any]:
    return {k: user_input[k] for k in (CONF_SPEAKER, CONF_LISTENER) if user_input.get(k)}


def _guess_listener(hass: HomeAssistant) -> dict[str, str]:
    """The app's silent speaker, when Music Assistant has one that is named like it."""
    for e in er.async_get(hass).entities.values():
        if (e.domain == "media_player" and e.platform != DOMAIN
                and (e.name or e.original_name or "").lower().startswith("vinyl id")):
            return {CONF_LISTENER: e.entity_id}
    return {}


class VinylConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            try:
                await fetch_status(self.hass, url)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Vinyl ID", data={CONF_URL: url},
                                               options=_options(user_input))
        schema = vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str, **PLAYERS})
        return self.async_show_form(
            step_id="user", errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or _guess_listener(self.hass)))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VinylOptionsFlow()


class VinylOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            old = self.config_entry.options.get(CONF_LISTENER)
            if old != user_input.get(CONF_LISTENER):       # no longer the listener: show it again
                set_listener_hidden(self.hass, old, False)
            return self.async_create_entry(data=_options(user_input))
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(PLAYERS), self.config_entry.options))
