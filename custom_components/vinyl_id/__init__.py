"""Vinyl ID: a Record Player media player for what the Vinyl ID app hears.

The app listens and identifies. This integration shows the result as one media player
(track, cover, progress) whose volume and power buttons act on the speaker you choose.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_LISTENER
from .coordinator import VinylCoordinator

PLATFORMS = [Platform.MEDIA_PLAYER]

type VinylConfigEntry = ConfigEntry[VinylCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VinylConfigEntry) -> bool:
    coordinator = VinylCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    set_listener_hidden(hass, entry.options.get(CONF_LISTENER), True)
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VinylConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: VinylConfigEntry) -> None:
    set_listener_hidden(hass, entry.options.get(CONF_LISTENER), False)


def set_listener_hidden(hass: HomeAssistant, entity_id: str | None, hidden: bool) -> None:
    """Keep the silent speaker the app listens through out of dashboards and pickers.

    It stays in Settings > Entities and keeps working. Only our own hiding is ever undone:
    an entity you hid yourself stays hidden.
    """
    reg = er.async_get(hass)
    if not entity_id or (found := reg.async_get(entity_id)) is None:
        return
    if hidden and found.hidden_by is None:
        reg.async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
    elif not hidden and found.hidden_by is er.RegistryEntryHider.INTEGRATION:
        reg.async_update_entity(entity_id, hidden_by=None)


async def _reload(hass: HomeAssistant, entry: VinylConfigEntry) -> None:
    """A different speaker was picked."""
    await hass.config_entries.async_reload(entry.entry_id)
