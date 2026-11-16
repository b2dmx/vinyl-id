"""The record's path is put back after a restart, and direct/in-sync follows who is listening."""
from pytest_homeassistant_custom_component.common import async_mock_service

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.vinyl_id import routing
from custom_components.vinyl_id.routing import Router


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(routing, "IDLE_FOR", 0.0)

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


async def test_paused_or_switched_off_is_left_alone(hass):
    cast(hass, state="paused", source="Queue", members=())
    assert await calls_of(hass) == []
    cast(hass, state="off", source="Queue", members=())
    assert await calls_of(hass) == []


async def test_a_player_that_only_just_went_idle_is_given_a_moment(hass, monkeypatch):
    monkeypatch.setattr(routing, "IDLE_FOR", 10.0)
    cast(hass, state="idle", source="Queue", members=())          # idle since this instant: maybe between songs
    hass.states.async_set(EAR, "idle")
    join = async_mock_service(hass, "media_player", "join")
    router = Router(hass, CAST, LINE_IN, EAR, None)
    await router.check()
    assert not join and router._later is not None                 # nothing yet, and a later look is booked
    router._cancel_later()


async def test_a_join_that_keeps_failing_is_tried_less_and_less(hass):
    cast(hass, members=(CAST,))
    hass.states.async_set(EAR, "idle")

    async def refuse(call):
        raise HomeAssistantError("that speaker cannot be grouped")

    hass.services.async_register("media_player", "join", refuse)
    router = Router(hass, CAST, LINE_IN, EAR, None)
    await router.check()                                          # no exception escapes
    assert router._retry == 2 * routing.RETRY_AFTER


async def test_a_change_during_a_check_is_looked_at_afterwards(hass):
    cast(hass)
    hass.states.async_set(EAR, "idle")
    hass.states.async_set(MONITOR, DIRECT, {"options": [DIRECT, SYNC]})
    seen = []

    async def select(call):
        seen.append(call.data["option"])
        if len(seen) == 1:                                        # while this call runs, the kitchen leaves again
            cast(hass)
            hass.states.async_set(MONITOR, SYNC, {"options": [DIRECT, SYNC]})
            await router.check()                                  # as the state change would: it is busy

    hass.services.async_register("select", "select_option", select)
    router = Router(hass, CAST, LINE_IN, EAR, MONITOR)
    cast(hass, members=(CAST, EAR, KITCHEN))
    await router.check()
    assert seen == [SYNC, DIRECT]


async def test_a_switch_that_will_not_switch_is_left_for_a_while(hass):
    cast(hass, members=(CAST, EAR, KITCHEN))
    hass.states.async_set(EAR, "idle")
    hass.states.async_set(MONITOR, DIRECT, {"options": [DIRECT, SYNC]})
    tries = []

    async def refuse(call):
        tries.append(1)
        raise HomeAssistantError("no")

    hass.services.async_register("select", "select_option", refuse)
    router = Router(hass, CAST, LINE_IN, EAR, MONITOR)
    await router.check()
    await router.check()
    assert len(tries) == 1 and router._retry == routing.RETRY_AFTER      # the record's own back-off is untouched


async def test_connected_on_paper_but_the_app_hears_nothing(hass):
    cast(hass)                                                     # playing its line-in, listener grouped
    hass.states.async_set(EAR, "playing")
    join = async_mock_service(hass, "media_player", "join")
    silent = {"now": False}
    router = Router(hass, CAST, LINE_IN, EAR, None, lambda: silent["now"])
    await router.check()
    assert not join                                               # the app hears the record: nothing to do
    silent["now"] = True
    await router.check()
    await router.check()                                          # and not again at once
    assert len(join) == 1 and join[0].data["group_members"] == [EAR]


async def test_calls_that_go_through_but_change_nothing_are_made_less_and_less(hass, monkeypatch):
    cast(hass, state="idle", source="Queue", members=())           # stays idle whatever is asked
    hass.states.async_set(EAR, "idle")
    join = async_mock_service(hass, "media_player", "join")
    async_mock_service(hass, "media_player", "select_source")
    clock = {"t": 1000.0}
    monkeypatch.setattr(routing.time, "monotonic", lambda: clock["t"])
    router = Router(hass, CAST, LINE_IN, EAR, None)
    waits = []
    for _ in range(4):
        await router.check()
        waits.append(router._retry)
        clock["t"] += router._retry + 1
    assert len(join) == 4 and waits == [45.0, 90.0, 180.0, 360.0]
    cast(hass)                                                     # it worked at last
    await router.check()
    assert router._retry == routing.RETRY_AFTER


async def test_the_app_hearing_nothing_for_good_is_not_rejoined_every_minute(hass, monkeypatch):
    cast(hass)
    hass.states.async_set(EAR, "playing")
    join = async_mock_service(hass, "media_player", "join")
    clock = {"t": 1000.0}
    monkeypatch.setattr(routing.time, "monotonic", lambda: clock["t"])
    router = Router(hass, CAST, LINE_IN, EAR, None, lambda: True)
    for _ in range(20):                                            # twenty minutes of ticks
        await router.check()
        clock["t"] += 60
    assert len(join) <= 5


async def test_the_monitor_is_set_by_what_its_options_are_called(hass):
    pick = Router._monitor_option
    named = ["Off", "Direct (no delay)", "Through Music Assistant (in sync)"]
    assert pick(named, in_sync=False) == named[1] and pick(named, in_sync=True) == named[2]
    assert pick(["In sync", "Direct"], in_sync=True) == "In sync" and pick(["In sync", "Direct"], in_sync=False) == "Direct"
    assert pick(["Local", "Network"], in_sync=False) == "Local" and pick(["Local", "Network"], in_sync=True) == "Network"
    assert pick(["Direct", "Network"], in_sync=True) == "Network"
    assert pick(["A", "B", "C"], in_sync=True) is None and pick(["A", "B", "C"], in_sync=False) is None

    cast(hass, members=(CAST, EAR, KITCHEN))
    hass.states.async_set(EAR, "idle")
    hass.states.async_set(MONITOR, "A", {"options": ["A", "B", "C"]})
    option = async_mock_service(hass, "select", "select_option")
    await Router(hass, CAST, LINE_IN, EAR, MONITOR).check()
    assert not option                                              # not understood: left alone


async def test_nothing_more_is_done_after_unloading_or_while_stopping(hass):
    cast(hass, state="idle", source="Queue", members=())
    hass.states.async_set(EAR, "idle")
    join = async_mock_service(hass, "media_player", "join")
    seen = []

    async def select(call):                                        # unloaded while this call is under way
        seen.append(1)
        router.stop()

    hass.services.async_register("media_player", "select_source", select)
    router = Router(hass, CAST, LINE_IN, EAR, None)
    await router.check()
    assert seen == [1] and not join                                # the join that would follow is not made
    router._check_in(5)
    assert router._later is None
    await router.check()
    assert seen == [1]

    other = Router(hass, CAST, LINE_IN, EAR, None)
    hass.set_state(__import__("homeassistant.core", fromlist=["CoreState"]).CoreState.stopping)
    await other.check()
    assert seen == [1] and not join
