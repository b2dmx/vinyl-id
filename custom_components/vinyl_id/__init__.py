"""Vinyl ID: a Record Player media player for what the Vinyl ID app hears.

The app listens and identifies. This integration shows the result as one media player
(track, cover, progress) whose volume and power buttons act on the speaker you choose.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import VinylCoordinator

PLATFORMS = [Platform.MEDIA_PLAYER]

type VinylConfigEntry = ConfigEntry[VinylCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VinylConfigEntry) -> bool:
    coordinator = VinylCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VinylConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _reload(hass: HomeAssistant, entry: VinylConfigEntry) -> None:
    """A different speaker was picked."""
    await hass.config_entries.async_reload(entry.entry_id)
