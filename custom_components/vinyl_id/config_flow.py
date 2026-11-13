"""Set-up: where the Vinyl ID app is, and which speaker plays the record."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_URL
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_SPEAKER, DEFAULT_URL, DOMAIN
from .coordinator import fetch_status

SPEAKER = selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))


def _options(user_input: dict[str, Any]) -> dict[str, Any]:
    return {CONF_SPEAKER: user_input[CONF_SPEAKER]} if user_input.get(CONF_SPEAKER) else {}


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
        schema = vol.Schema({
            vol.Required(CONF_URL, default=DEFAULT_URL): str,
            vol.Optional(CONF_SPEAKER): SPEAKER,
        })
        return self.async_show_form(
            step_id="user", errors=errors,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VinylOptionsFlow()


class VinylOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_options(user_input))
        schema = vol.Schema({vol.Optional(CONF_SPEAKER): SPEAKER})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options))
