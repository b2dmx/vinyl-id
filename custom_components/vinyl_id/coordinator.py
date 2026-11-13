"""Asks the Vinyl ID app what is playing."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_SECONDS

_LOGGER = logging.getLogger(__name__)


async def fetch_status(hass: HomeAssistant, url: str) -> dict[str, Any]:
    """The app's status. Raises aiohttp.ClientError, TimeoutError or ValueError."""
    async with asyncio.timeout(5):
        resp = await async_get_clientsession(hass).get(f"{url.rstrip('/')}/api/status")
        resp.raise_for_status()
        data = await resp.json()
    if not isinstance(data, dict) or "playing" not in data:
        raise ValueError("Not a Vinyl ID app")
    return data


class VinylCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=timedelta(seconds=POLL_SECONDS))
        self.url: str = entry.data[CONF_URL]

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await fetch_status(self.hass, self.url)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise UpdateFailed(f"Vinyl ID app at {self.url} did not answer: {err}") from err
