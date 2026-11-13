<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-white.png">
    <img src="assets/logo.png" height="150" alt="Vinyl ID">
  </picture>
</p>

<p align="center"><b>Shows what's spinning on your record player, with no microphone.</b></p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fvinyl-id"><img src="https://img.shields.io/badge/Home%20Assistant-App-41BDF5?logo=homeassistant&logoColor=white" alt="Home Assistant app"></a>
  <a href="https://ko-fi.com/goobis2dmx"><img src="https://img.shields.io/badge/Ko--fi-Support-FF5E5B?logo=kofi&logoColor=white" alt="Support on Ko-fi"></a>
</p>

Vinyl ID listens to your record player's line feed and shows the song, artist, album and cover in Home Assistant. It comes in two parts:

- **The app** listens, identifies the songs and keeps your library.
- **The integration** adds a **Record Player** media player: song, cover and progress, with the volume and power of the speaker you choose.

## What it does

- **Identifies songs** with Shazam once, then from its own library.
- **Learns your records**, so replays work offline.
- **Shows the cover** in Home Assistant and on displays.
- **Follows an album.** Add it from MusicBrainz (tracks, sides, cover), pick **Now spinning**, and it knows the track, the side and where you are in it, flipping to side B on its own.

## Installation

Needs Home Assistant with the Mosquitto broker.

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fvinyl-id)

1. Add the repository, install **Vinyl ID**, and pick an **Audio source**: Sendspin, AirPlay or Stream.
2. Start it, then open **Vinyl ID** in the sidebar.
3. For the Record Player, add this repository to HACS as an **Integration**, download **Vinyl ID**, restart Home Assistant, then add **Vinyl ID** under Settings > Devices & services. Pick the speaker that plays your record, or leave it empty to only show what is playing.

**On a NAS or server instead:** fill in `docker-compose.yml` (your MQTT broker and login, `PUBLIC_URL`), then run `docker compose up -d --build`. Home Assistant finds it through MQTT and the same entities appear. Open `http://<your-nas>:8095` for the library page. Anyone on your network can open it and, unless you set `EDIT_FROM` to the addresses allowed to make changes, edit your library.

## Which entity is which

- **Record Player** (from the integration) is the one to look at and put on dashboards: song, cover, progress, and your speaker's volume.
- **Record Playing**, **Now Playing** and **Album Art** (from the app) carry the same information for automations and displays.
- **Vinyl ID Listener** is a silent speaker in Music Assistant. It is how the record reaches the app, so it says "playing" whenever the line-in is on, even with no record. Send the record to it, then ignore it. The integration hides it from dashboards for you.

## Keeping the record connected

With a line-in player such as an Apollo CAST, the record reaches Vinyl ID as playback in Music Assistant, and a restart of anything in the chain drops it. Tell the integration which player has the line-in and it puts the connection back by itself, within a minute. It never interrupts other music on that player.

To hear the record in more rooms, group those speakers onto the line-in player as you would for any music. If the player has a direct/in-sync switch for its line-in, Vinyl ID uses direct while only that player is listening and in sync once other rooms join.

## Onboarding

Drop the needle. **Record Playing** turns on and **Now Playing** shows the song. If Shazam doesn't know it, name it in the sidebar and Vinyl ID learns it.

## Tips

- **Silence level:** **Input Level** shows how loud the signal is. If quiet passages count as silence, set the signal threshold a little below them. A record only counts as playing once the sound is 12 dB above the threshold, so hum and hiss don't start it.
- **Learn my records** is on by default. Turn it off to always use Shazam.
- The library can only be changed from the Vinyl ID page in Home Assistant. To let a script or another computer change it too, list its address under **Also allow edits from**.
- Covers you set are never replaced automatically. A box set can have a cover for each LP.
- To see the song on your speakers, play `http://<this app>:<port>/stream.mp3` on them instead of the line-in: it is the same record, as a stream that names its songs.
- Songs that run together not picked up? Lower `RECHECK_SECONDS`.
- Space: a cover is about 50-150 KB and a learned song about 0.2 MB, so 1,000 songs is roughly 250 MB. It all lives in the data folder.
- Example automations are in [ha](ha).

## License

The code is [MIT](LICENSE). The Vinyl ID logo and icons (everything in `assets/` and `custom_components/vinyl_id/brand/`, plus `app/icon.png`, `app/logo.png` and `app/vinyl_id/ui/logo.svg`) are © Goobis, all rights reserved. They are not covered by the MIT license, so please don't reuse them without permission.

---

<p align="center">
  <a href="LICENSE">MIT</a> · <a href="https://ko-fi.com/goobis2dmx">Support on Ko-fi</a><br><br>
  Vinyl ID v0.7.0 by Goobis<br>
  AI was used to help develop this application
</p>
