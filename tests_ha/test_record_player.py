from datetime import timedelta

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
    aioclient_mock.get(f"{URL}/api/status", json=data)
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
    assert a["media_duration"] == 200 and 29 <= a["media_position"] <= 32
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
    aioclient_mock.get("http://nothing.test/api/status", status=404)
    aioclient_mock.get(f"{URL}/api/status", json=status())
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: "http://nothing.test"})
    assert r["errors"] == {"base": "cannot_connect"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_URL: URL + "/", CONF_SPEAKER: SPEAKER})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"] == {CONF_URL: URL} and r["options"] == {CONF_SPEAKER: SPEAKER}
