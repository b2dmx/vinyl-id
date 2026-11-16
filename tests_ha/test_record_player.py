from datetime import timedelta

import voluptuous as vol
from homeassistant.components.media_player import MediaPlayerEntityFeature as F
from homeassistant.const import CONF_URL
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vinyl_id.const import CONF_SPEAKER, DOMAIN

URL = "http://app.test:8780"
PLAYER = "media_player.record_player"
SPEAKER = "media_player.amp"


def status(playing=True, **now):
    started = (dt_util.utcnow() - timedelta(seconds=30)).isoformat(timespec="seconds")
    base = {"id": 1, "title": "Song Two", "artist": "The Band", "album": "Test LP", "album_id": 1,
            "art_url": f"{URL}/api/albums/1/art?v=1", "source": "spin", "side": "A", "track_number": 2,
            "side_tracks": 5, "album_tracks": 10, "track_length": 200.4, "track_started_at": started}
    return {"playing": playing, "unknown": False, "now": (base | now) if playing else None,
            "library": 3, "learn": True, "spin": None}


async def setup(hass, aioclient_mock, data, speaker=None):
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=data)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL}, options={CONF_SPEAKER: speaker} if speaker else {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_shows_the_record(hass, aioclient_mock):
    await setup(hass, aioclient_mock, status())
    s = hass.states.get(PLAYER)
    assert s.state == "playing"
    a = s.attributes
    assert (a["media_title"], a["media_artist"], a["media_album_name"]) == ("Song Two", "The Band", "Test LP")
    assert a["media_duration"] == 200 and a["media_position"] == 0
    assert 29 <= (dt_util.utcnow() - a["media_position_updated_at"]).total_seconds() <= 33   # 30 s into the track
    assert a["side"] == "A" and a["media_track"] == 2 and a["identified_by"] == "spin"
    assert a["entity_picture"].startswith("/api/media_player_proxy/")
    assert a["supported_features"] == 0          # no speaker chosen: display only


async def test_idle_when_nothing_plays(hass, aioclient_mock):
    await setup(hass, aioclient_mock, status(playing=False))
    s = hass.states.get(PLAYER)
    assert s.state == "idle" and "media_title" not in s.attributes


async def test_volume_goes_to_the_chosen_speaker(hass, aioclient_mock):
    hass.states.async_set(SPEAKER, "on", {"supported_features": int(F.VOLUME_SET | F.VOLUME_MUTE | F.PLAY),
                                         "volume_level": 0.4, "is_volume_muted": False})
    calls = []
    hass.bus.async_listen("call_service", lambda e: calls.append(e.data["service_data"]))
    await setup(hass, aioclient_mock, status(), speaker=SPEAKER)
    s = hass.states.get(PLAYER)
    assert s.attributes["supported_features"] == int(F.VOLUME_SET | F.VOLUME_MUTE)   # not PLAY
    assert s.attributes["volume_level"] == 0.4
    await hass.services.async_call("media_player", "volume_set",
                                   {"entity_id": PLAYER, "volume_level": 0.2}, blocking=True)
    await hass.async_block_till_done()
    sent = [c for c in calls if c.get("entity_id") == SPEAKER]
    assert sent and sent[0]["volume_level"] == 0.2
    hass.states.async_set(SPEAKER, "on", {"supported_features": int(F.VOLUME_SET), "volume_level": 0.7})
    await hass.async_block_till_done()
    assert hass.states.get(PLAYER).attributes["volume_level"] == 0.7                 # follows the speaker


async def test_setup_form(hass, aioclient_mock):
    aioclient_mock.get("http://nothing.test/api/status?tracks=0", status=404)
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "http://nothing.test"})
    assert r["errors"] == {"base": "cannot_connect"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL + "/", CONF_SPEAKER: SPEAKER})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"] == {CONF_URL: URL} and r["options"] == {CONF_SPEAKER: SPEAKER}


async def test_listener_is_hidden_and_shown_again(hass, aioclient_mock):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    ear = reg.async_get_or_create("media_player", "music_assistant", "ear", suggested_object_id="vinyl_id_listener",
                                  original_name="Vinyl ID Listener").entity_id
    mine = reg.async_get_or_create("media_player", "music_assistant", "mine", suggested_object_id="hidden_by_me").entity_id
    reg.async_update_entity(mine, hidden_by=er.RegistryEntryHider.USER)
    hass.states.async_set("media_player.cast", "idle", {"source_list": ["Queue", "Sendspin Source (CAST)"]})

    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL, "source_player": "media_player.cast"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "line_in"
    suggested = {k.schema: (k.description or {}).get("suggested_value") for k in r["data_schema"].schema}
    assert suggested["source_name"] == "Sendspin Source (CAST)" and suggested["listener"] == ear   # both found
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"source_name": "Sendspin Source (CAST)", "listener": ear, "advanced": {}})
    await hass.async_block_till_done()
    entry = r["result"]
    assert r["options"] == {"source_player": "media_player.cast", "source_name": "Sendspin Source (CAST)", "listener": ear}
    assert reg.async_get(ear).hidden_by is er.RegistryEntryHider.INTEGRATION

    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"source_player": "media_player.cast"})
    await hass.config_entries.options.async_configure(
        r["flow_id"], {"source_name": "Sendspin Source (CAST)", "listener": mine, "advanced": {"monitor": "select.x"}})
    await hass.async_block_till_done()
    assert reg.async_get(ear).hidden_by is None                                 # shown again
    assert reg.async_get(mine).hidden_by is er.RegistryEntryHider.USER          # your own choice is kept
    assert entry.options["monitor"] == "select.x"

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get(mine).hidden_by is er.RegistryEntryHider.USER


async def test_the_address_can_be_changed_later(hass, aioclient_mock):
    entry = await setup(hass, aioclient_mock, status())
    new = "http://moved.test:8780"
    aioclient_mock.get(f"{new}/api/status?tracks=0", json=status())
    r = await entry.start_reconfigure_flow(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reconfigure"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: new + "/"})
    await hass.async_block_till_done()
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == new


async def test_a_renamed_listener_is_found_by_the_name_the_app_gives(hass, aioclient_mock):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    reg.async_get_or_create("media_player", "music_assistant", "amp", suggested_object_id="turntable_ear_amp",
                            original_name="Turntable Ear Amp")          # a longer name is someone else
    ear = reg.async_get_or_create("media_player", "music_assistant", "ear", suggested_object_id="turntable_ear",
                                  original_name="Turntable Ear").entity_id
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status() | {"listener": "Turntable Ear"})
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL})
    await hass.async_block_till_done()
    assert r["options"] == {"listener": ear}
    assert reg.async_get(ear).hidden_by is er.RegistryEntryHider.INTEGRATION


async def test_the_cover_is_fetched_from_the_address_home_assistant_uses(hass, aioclient_mock):
    await setup(hass, aioclient_mock, status(art_url="http://localhost:8780/api/albums/1/art?v=7"))
    player = hass.data["entity_components"]["media_player"].get_entity(PLAYER) if "entity_components" in hass.data else None
    from homeassistant.helpers import entity_component  # noqa: F401
    ent = hass.data["domain_entities"]["media_player"][PLAYER] if player is None else player
    assert ent.media_image_url == f"{URL}/api/albums/1/art?v=7"


def suggested(result):
    out = {}
    for k, v in result["data_schema"].schema.items():
        if hasattr(v, "schema") and isinstance(getattr(v, "schema", None), vol.Schema):     # a section
            out |= {i.schema: (i.description or {}).get("suggested_value") for i in v.schema.schema}
        else:
            out[k.schema] = (k.description or {}).get("suggested_value")
    return out


async def test_a_line_in_under_another_name_is_found_but_configure_does_not_guess(hass, aioclient_mock):
    from custom_components.vinyl_id.config_flow import _guess_player, _line_in

    hass.states.async_set("media_player.tv", "on", {"source_list": ["HDMI 1", "Netflix"]})
    hass.states.async_set("media_player.streamer", "idle", {"source_list": ["Queue", "Line-In"]})
    assert _guess_player(hass) == "media_player.streamer" and _line_in(["Queue", "Line-In"]) == "Line-In"
    hass.states.async_set("media_player.cast", "idle", {"source_list": ["Sendspin Source (CAST)"]})
    assert _guess_player(hass) == "media_player.cast"                         # the usual name wins
    assert _guess_player(hass, never="media_player.cast") == "media_player.streamer"

    entry = await setup(hass, aioclient_mock, status())                       # set up with no line-in player
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert suggested(r)["source_player"] is None                              # left empty stays empty
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"] == {}


async def test_the_guessed_line_in_player_is_never_the_speaker_itself(hass, aioclient_mock):
    hass.states.async_set(SPEAKER, "idle", {"source_list": ["Wifi", "Line In"]})    # no Music Assistant here
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert suggested(r)["source_player"] == SPEAKER                                 # all there is to guess
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {CONF_URL: URL, CONF_SPEAKER: SPEAKER, "source_player": SPEAKER})
    assert r["step_id"] == "line_in" and [k.schema for k in r["data_schema"].schema] == ["speaker_source"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["options"] == {CONF_SPEAKER: SPEAKER}


async def test_the_saved_input_survives_a_speaker_that_cannot_list_its_inputs(hass, aioclient_mock):
    hass.states.async_set(SPEAKER, "unavailable")
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status(playing=False))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL},
                            options={CONF_SPEAKER: SPEAKER, "speaker_source": "Optical"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {CONF_SPEAKER: SPEAKER})
    assert r["type"] is FlowResultType.CREATE_ENTRY                     # no second form: nothing to ask
    assert r["data"] == {CONF_SPEAKER: SPEAKER, "speaker_source": "Optical"}

    # ... also when the second form is shown, for the line-in, without that field
    hass.states.async_set("media_player.cast", "idle", {"source_list": ["Line In"]})
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {CONF_SPEAKER: SPEAKER, "source_player": "media_player.cast"})
    assert r["step_id"] == "line_in" and "speaker_source" not in suggested(r)
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"source_name": "Line In", "listener": "media_player.ear", "advanced": {}})
    assert r["data"]["speaker_source"] == "Optical"

    # an input you emptied yourself is gone, and another speaker starts without one
    hass.states.async_set(SPEAKER, "idle", {"source_list": ["Wifi", "Optical"]})
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {CONF_SPEAKER: SPEAKER})
    assert suggested(r)["speaker_source"] == "Optical"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["data"] == {CONF_SPEAKER: SPEAKER, "listener": "media_player.ear"}


async def test_the_monitor_is_guessed_once_and_stays_empty_when_emptied(hass, aioclient_mock):
    hass.states.async_set("media_player.cast", "idle", {"source_list": ["Line In"]})
    hass.states.async_set("select.cast_line_in_monitor", "Direct", {"options": ["Direct", "In sync"]})
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status(playing=False))
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL, "source_player": "media_player.cast"})
    assert suggested(r)["monitor"] == "select.cast_line_in_monitor"          # first set-up: found for you
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"source_name": "Line In", "listener": "media_player.ear", "advanced": {}})   # ... and emptied
    await hass.async_block_till_done()
    entry = r["result"]
    assert "monitor" not in entry.options
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"source_player": "media_player.cast"})
    assert suggested(r)["monitor"] is None                                    # not filled in again


async def test_a_listener_you_show_again_stays_shown(hass, aioclient_mock):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    ear = reg.async_get_or_create("media_player", "music_assistant", "ear", suggested_object_id="ear").entity_id
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status(playing=False))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL}, options={"listener": ear})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get(ear).hidden_by is er.RegistryEntryHider.INTEGRATION
    reg.async_update_entity(ear, hidden_by=None)                              # you want to see it
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get(ear).hidden_by is None


async def test_of_two_players_with_the_listeners_name_music_assistants_is_it(hass, aioclient_mock):
    from homeassistant.helpers import entity_registry as er

    from custom_components.vinyl_id.config_flow import _find_listener

    reg = er.async_get(hass)
    reg.async_get_or_create("media_player", "sendspin", "ear", suggested_object_id="ear_other",
                            original_name="Vinyl ID Listener")
    ear = reg.async_get_or_create("media_player", "music_assistant", "ear", suggested_object_id="ear_ma",
                                  original_name="Vinyl ID Listener").entity_id
    assert _find_listener(hass, "Vinyl ID Listener") == ear and _find_listener(hass) == ear


async def test_a_new_address_is_used_at_once_while_the_app_could_not_be_reached(hass, aioclient_mock):
    from homeassistant.config_entries import ConfigEntryState

    aioclient_mock.get(f"{URL}/api/status?tracks=0", status=500)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    new = "http://moved.test:8780"
    aioclient_mock.get(f"{new}/api/status?tracks=0", json=status())
    r = await entry.start_reconfigure_flow(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: new})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def record_starts(hass, aioclient_mock, speaker_state, attrs):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

    hass.states.async_set(SPEAKER, speaker_state, attrs)
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status(playing=False))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL},
                            options={CONF_SPEAKER: SPEAKER, "speaker_source": "Optical"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    select = async_mock_service(hass, "media_player", "select_source")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    return entry, select


async def test_a_speaker_that_was_away_is_switched_when_it_is_back(hass, aioclient_mock):
    _, select = await record_starts(hass, aioclient_mock, "unavailable", {})
    assert not select
    hass.states.async_set(SPEAKER, "idle", {"source": "Wifi"})
    await hass.async_block_till_done()
    assert [c.data["source"] for c in select] == ["Optical"]
    hass.states.async_set(SPEAKER, "playing", {"source": "Wifi"})               # you chose otherwise: yours now
    await hass.async_block_till_done()
    assert len(select) == 1


async def test_a_speaker_that_was_off_is_asked_then_and_again_when_it_comes_on(hass, aioclient_mock):
    _, select = await record_starts(hass, aioclient_mock, "off", {"source": "Wifi"})
    assert len(select) == 1
    hass.states.async_set(SPEAKER, "off", {"source": "Wifi", "volume_level": 0.2})
    await hass.async_block_till_done()
    assert len(select) == 1                                                     # not over and over while it is off
    hass.states.async_set(SPEAKER, "idle", {"source": "Wifi"})
    await hass.async_block_till_done()
    assert len(select) == 2


async def test_set_up_in_the_middle_of_a_record_switches_nothing(hass, aioclient_mock):
    from pytest_homeassistant_custom_component.common import async_mock_service

    hass.states.async_set(SPEAKER, "idle", {"source": "Wifi"})
    select = async_mock_service(hass, "media_player", "select_source")
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())             # already playing
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL},
                            options={CONF_SPEAKER: SPEAKER, "speaker_source": "Optical"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(SPEAKER, "playing", {"source": "Wifi"})
    await hass.async_block_till_done()
    assert not [c for c in select if c.data.get("source") == "Optical"]


async def test_the_speaker_is_put_on_the_records_input_when_a_record_starts(hass, aioclient_mock):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

    hass.states.async_set(SPEAKER, "idle", {"source": "Wifi", "source_list": ["Wifi", "Optical"]})
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status(playing=False))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL},
                            options={CONF_SPEAKER: SPEAKER, "speaker_source": "Optical"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    select = async_mock_service(hass, "media_player", "select_source")   # after set-up, which registers the real one

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert [(c.data["entity_id"], c.data["source"]) for c in select] == [(SPEAKER, "Optical")]


async def test_the_second_form_offers_the_speakers_inputs(hass, aioclient_mock):
    hass.states.async_set(SPEAKER, "idle", {"source_list": ["Wifi", "Optical"]})
    aioclient_mock.get(f"{URL}/api/status?tracks=0", json=status())
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL, CONF_SPEAKER: SPEAKER})
    assert r["step_id"] == "line_in" and [k.schema for k in r["data_schema"].schema] == ["speaker_source"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"speaker_source": "Optical"})
    assert r["options"] == {CONF_SPEAKER: SPEAKER, "speaker_source": "Optical"}
