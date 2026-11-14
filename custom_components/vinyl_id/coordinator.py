"""Asks the Vinyl ID app what is playing."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_SECONDS

_LOGGER = logging.getLogger(__name__)
NO_INPUT_DB = -88
SILENT_FOR = 30.0      # seconds without any input before the connection is doubted


async def fetch_status(hass: HomeAssistant, url: str) -> dict[str, Any]:
    """The app's status. Raises aiohttp.ClientError, TimeoutError or ValueError."""
    async with asyncio.timeout(5):
        resp = await async_get_clientsession(hass).get(f"{url.rstrip('/')}/api/status", params={"tracks": "0"})
        resp.raise_for_status()
        data = await resp.json()
    if not isinstance(data, dict) or "playing" not in data:
        raise ValueError("Not a Vinyl ID app")
    return data


class VinylCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=timedelta(seconds=POLL_SECONDS), always_update=False)
        self.url: str = entry.data[CONF_URL]
        self._silent_since: float | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await fetch_status(self.hass, self.url)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self._silent_since = None
            raise UpdateFailed(f"Vinyl ID app at {self.url} did not answer: {err}") from err
        # At or below -88 dB nothing is arriving at the app: not even the turntable's hum.
        if data.get("level", 0) <= NO_INPUT_DB:
            self._silent_since = self._silent_since or time.monotonic()
        else:
            self._silent_since = None
        return data

    def hears_nothing(self) -> bool:
        """The app has had no input at all for a while (its connection is only there on paper)."""
        return self._silent_since is not None and time.monotonic() - self._silent_since >= SILENT_FOR
