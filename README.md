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

A Home Assistant app. It listens to your record player's line feed (for example from an Apollo CAST PRO-1), shows the song, artist, album and cover in Home Assistant, and learns your records so replays are identified locally.

## What it does

- **Identifies the song** with Shazam the first time, then from its own library after that.
- **Learns your records.** About 0.2 MB per song, and a wrong guess never sticks.
- **Shows the cover** in Home Assistant and on displays such as the Apollo M-1.
- **Runs locally.** Once learned, replays need no internet.

## Installation

Needs Home Assistant with the Mosquitto broker, and a way to send the record player's audio to it.

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fvinyl-id)

1. Add the repository (button above), install **Vinyl ID**, and start it. It uses your Mosquitto broker automatically.
2. Choose an **Audio source** on the Configuration tab:
   - **Sendspin**: shows up in Music Assistant as a player.
   - **AirPlay**: shows up as an AirPlay speaker.
   - **Stream**: reads a stream address directly.

Docker (NAS or any server): fill in `docker-compose.yml` (broker, login, `AUDIO_SOURCE`), then run `docker compose up -d --build`.

## Onboarding

Open **Vinyl ID** in the Home Assistant sidebar, then drop the needle.

- **Record Playing** turns on a few seconds after the needle drops, and off after 45 s of silence. Flipping a side doesn't count as stopping.
- **Now Playing** shows the song, with artist, album, art URL and source (`local`, `shazam`, `manual` or `unknown`) as attributes.
- **Album Art**, **Library Size**, **Forget Current Song** and **Input Level** round out the entities.
- Unknown record? Name it in the sidebar and Vinyl ID learns it from what it just heard.

## Tips

- Play a quiet record, watch **Input Level**, and set the signal threshold about 10 dB below the quiet parts.
- **Learn my records** is on by default. Turn it off to always use Shazam; covers and edits are still saved.
- Covers you set are locked and never replaced automatically.
- Songs that run straight into each other (live albums, medleys) not picked up? Lower `RECHECK_SECONDS`.
- Vinyl ID's speaker is silent: it only listens. Its data folder is included in Home Assistant backups.
- Example automations to merge into your own are in [ha](ha).

---

<p align="center">
  <a href="LICENSE">MIT</a> · <a href="https://ko-fi.com/goobis2dmx">Support on Ko-fi</a><br><br>
  Vinyl ID v0.4.0 by Goobis<br>
  AI was used to help develop this application
</p>
