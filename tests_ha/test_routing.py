"""The record's path is put back after a restart, and direct/in-sync follows who is listening."""
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.vinyl_id.routing import Router

CAST, EAR, MONITOR, KITCHEN = "media_player.cast", "media_player.ear", "select.monitor", "media_player.kitchen"
LINE_IN = "Line In Source"
DIRECT, SYNC = "Direct", "In sync"


def cast(hass, state="playing", source=LINE_IN, members=(CAST, EAR)):
    hass.states.async_set(CAST, state, {"source": source, "group_members": list(members),
                                         "source_list": ["Queue", LINE_IN]})


async def calls_of(hass, *, monitor=DIRECT):
    mocks = [async_mock_service(hass, d, s) for d, s in
             (("media_player", "select_source"), ("media_player", "join"), ("select", "select_option"))]
    hass.states.async_set(EAR, "idle")
    hass.states.async_set(MONITOR, monitor, {"options": [DIRECT, SYNC]})
    router = Router(hass, CAST, LINE_IN, EAR, MONITOR)
    await router.check()
    await hass.async_block_till_done()
    seen = sorted((c for m in mocks for c in m), key=lambda c: c.service)   # by name: the mocks do not record the order
    return [(c.service, {k: v for k, v in c.data.items() if k != "entity_id"}) for c in seen]


async def test_reconnects_after_a_restart(hass):
    cast(hass, state="idle", source="Queue", members=())
    assert await calls_of(hass) == [("join", {"group_members": [EAR]}), ("select_source", {"source": LINE_IN})]


async def test_only_rejoins_when_the_line_in_still_plays(hass):
    cast(hass, members=(CAST,))
    assert await calls_of(hass) == [("join", {"group_members": [EAR]})]


async def test_never_interrupts_other_music(hass):
    cast(hass, state="playing", source="Queue", members=())
    assert await calls_of(hass) == []


async def test_waits_for_a_device_that_is_away(hass):
    hass.states.async_set(CAST, "unavailable")
    assert await calls_of(hass) == []


async def test_direct_alone_and_in_sync_with_other_rooms(hass):
    cast(hass)
    assert await calls_of(hass, monitor=DIRECT) == []                                     # already right
    cast(hass, members=(CAST, EAR, KITCHEN))
    assert await calls_of(hass, monitor=DIRECT) == [("select_option", {"option": SYNC})]
    cast(hass)
    assert await calls_of(hass, monitor=SYNC) == [("select_option", {"option": DIRECT})]
