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

A Home Assistant app that listens to your record player's line feed and shows the song, artist, album and cover.

## What it does

- **Identifies songs** with Shazam once, then from its own library.
- **Learns your records**, so replays work offline.
- **Shows the cover** in Home Assistant and on displays.

## Installation

Needs Home Assistant with the Mosquitto broker.

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fvinyl-id)

1. Add the repository, install **Vinyl ID**, and pick an **Audio source**: Sendspin, AirPlay or Stream.
2. Start it, then open **Vinyl ID** in the sidebar.

Docker: fill in `docker-compose.yml`, then run `docker compose up -d --build`.

## Onboarding

Drop the needle. **Record Playing** turns on and **Now Playing** shows the song. If Shazam doesn't know it, name it in the sidebar and Vinyl ID learns it.

## Tips

- **Silence level:** **Input Level** shows how loud the signal is. If quiet passages count as silence, set the signal threshold a little below them.
- **Learn my records** is on by default. Turn it off to always use Shazam.
- Covers you set are never replaced automatically.
- Songs that run together not picked up? Lower `RECHECK_SECONDS`.
- Example automations are in [ha](ha).

---

<p align="center">
  <a href="LICENSE">MIT</a> · <a href="https://ko-fi.com/goobis2dmx">Support on Ko-fi</a><br><br>
  Vinyl ID v0.4.0 by Goobis<br>
  AI was used to help develop this application
</p>
