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

Vinyl ID listens to your record player and shows the song, artist, album and cover in Home Assistant. It needs no microphone: it listens to the same audio your speakers get. It comes in two parts:

- **The app** listens, identifies the songs and keeps your library.
- **The integration** adds a **Record Player** media player: song, cover and progress, with the volume and power of the speaker you choose.

## What it does

- **Identifies songs** with Shazam once, then from its own library.
- **Learns your records**, so replays work offline.
- **Shows the cover** in Home Assistant and on displays. A box set can have a cover for each LP.
- **Follows an album.** Add it from MusicBrainz (tracks, sides, cover) and it knows the track, the side and where you are in it, flipping to side B on its own. It starts following by itself when it recognises a song from an album you added.
- **Keeps the record connected** after restarts, and plays it direct or in sync with other rooms (see below).

## What you need

- Home Assistant with the **Mosquitto broker** app and the **MQTT** integration.
- A way to get the record's audio onto your network. The tested way is a line-in player that **Music Assistant** can play from, such as an Apollo CAST: turntable (with a phono preamp) into its Line In.
- For that way: the **Music Assistant** app with its **Sendspin** provider on, and the Music Assistant integration.
- **HACS** (or the Marketplace), for the integration.

## Installation

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fvinyl-id)

**1. The app**

1. Add the repository with the button above, install **Vinyl ID** and start it. The first install builds the app on your device, which takes several minutes. The default **Audio source**, Sendspin, is the tested one.
2. In Music Assistant a new speaker appears: **Vinyl ID Listener** (or the name you gave it on the app's Configuration tab). It is silent. It is how the record reaches the app.

**2. Send the record to the listener**

In Music Assistant, open your line-in player (the CAST), choose its line-in as the **source**, then **group** Vinyl ID Listener onto that player. The integration in step 3 does this for you from then on.

**3. The integration**

1. In HACS, add `https://github.com/b2dmx/vinyl-id` as a custom repository of type **Integration**, download **Vinyl ID**, and restart Home Assistant.
2. Settings > Devices & services > Add integration > **Vinyl ID**. Most fields are filled in for you:
   - **Address of the Vinyl ID app:** leave as it is.
   - **Speaker that plays your record:** the speaker or amp you listen on. The Record Player's volume and power buttons control it.
   - **The app's silent speaker:** Vinyl ID Listener.
   - **Player whose line-in the record is on** and **its line-in source:** your CAST and the source you chose in step 2. These two and the silent speaker work together: with all three filled in, Vinyl ID keeps the record connected.
   - **That player's direct / in-sync switch:** leave empty unless your player has such a setting.

**4. Check it hears the record**

Open the Vinyl ID page: on the app's page in Home Assistant press **Open Web UI**, and switch on **Show in sidebar** to keep it one click away. Drop the needle. At the bottom, the page shows how loud the input is and how loud it must be for a record to start. If the first number never rises while a record plays, the record is not reaching the listener: repeat step 2.

The song should appear within about 10 to 15 seconds on the page and on the **Record Player**. If Shazam doesn't know it, press **Name it** on the page and Vinyl ID learns it.

**Other audio sources**

- **Stream:** if something already serves the turntable as an audio stream, choose Stream and enter its address (for example `http://192.168.1.50:8000/turntable.mp3`). Music Assistant is not needed.
- **AirPlay:** Vinyl ID Listener appears as an AirPlay speaker instead. This path has not been tested on Home Assistant OS.

**On a NAS or server instead of Home Assistant OS:** download this repository (Code > Download ZIP, or `git clone`), fill in `docker-compose.yml` (your MQTT broker, a login for it, which you create in Home Assistant under Settings > People > Users, and `PUBLIC_URL`), then run `docker compose up -d --build`. The library page is at `http://<that machine>:8780`, and that is also the address to give the integration. Anyone on your network can open the page and, unless you set `EDIT_FROM`, change your library.

## Which entity is which

- **Record Player** (from the integration) is the one to look at and put on dashboards: song, cover, progress, and your speaker's volume.
- From the app, for automations and displays: **Record Playing**, **Now Playing**, **Album Art**, **Library Size**, **Input Level**, and a **Forget Current Song** button.
- **Vinyl ID Listener** is the silent speaker in Music Assistant. It says "playing" whenever the line-in is on, even with no record. The integration hides it from dashboards; it stays under Settings > Entities.

## Keeping the record connected

The record reaches Vinyl ID as playback in Music Assistant, so restarting the app, Music Assistant or the line-in player drops it. The integration puts it back by itself within a couple of minutes. It only does this while the line-in player is idle: it never takes the player away from other music, and leaves it alone when paused or switched off.

To hear the record in more rooms, group those speakers onto the line-in player in Music Assistant, as for any music. If the player has a direct/in-sync switch for its line-in, Vinyl ID uses direct while only that player is listening and in sync once other rooms join.

## Your library

Open the Vinyl ID page.

- **Add an album** from MusicBrainz to get its tracks, sides and cover. Pick the entry that matches your record (LP, year, label).
- **Now spinning:** choose an album and where the needle is, if Vinyl ID hasn't worked it out by itself.
- **Not right?** on the playing song corrects it, and Vinyl ID learns the correction.
- **Change cover**, or give each LP of a box set its own.
- **Track lengths** are measured as your record plays: from gap to gap, or from one recognized song to the next where songs run together. Until a track has been measured, the listed length (from MusicBrainz, often the CD's) stands in, shown with a ~. You can type a length yourself; it is then never replaced.
- **Learned songs:** rename a song, or forget one that was learned wrongly.
- **Covers of removed albums** are kept for 30 days at the bottom of the page, where you can put one back on an album or delete it.

## Tips

- **Signal threshold** (on the app's Configuration tab): the level of your turntable when it is on but not playing. The bottom of the page shows the input level; use the number you see with the turntable on and the needle up. A record starts once the sound is 12 dB above it, and a gap between songs is sound that falls back to it. Save and restart the app for a new threshold to apply.
- **Learn my records** is on by default. Turn it off to always use Shazam.
- The library can only be changed from the Vinyl ID page in Home Assistant. To let a script or another computer change it too, list its address under **Also allow edits from**. Anyone on your network can view the page and play the stream; only changes are restricted.
- Covers you set are never replaced automatically.
- `http://<home assistant>:8780/stream.mp3` is the record as a stream that names its songs, for a player that should show the title itself. It runs a few seconds behind the record.
- Space: a cover is about 50-150 KB and a learned song about 0.2 MB, so 1,000 songs is roughly 250 MB. The page shows the current size. **Library size limit** stops learning at a size you choose; songs already learned keep working.
- The app moved to another machine or port? Settings > Devices & services, open the **Vinyl ID** integration (not the Vinyl ID device under MQTT), then the three-dot menu > Reconfigure.

## Updating and removing

- **Update the app:** Settings > Apps > Vinyl ID > Update. **Update the integration:** HACS > Vinyl ID > Update, then restart Home Assistant.
- **Your library** lives in the app's data. It is included in Home Assistant backups of the app, and uninstalling the app deletes it.
- **To remove everything:** delete the Vinyl ID integration, remove it in HACS, uninstall the app, then delete the Vinyl ID device under MQTT.

## License

The code is [MIT](LICENSE). The Vinyl ID logo and icons (everything in `assets/` and `custom_components/vinyl_id/brand/`, plus `app/icon.png`, `app/logo.png` and `app/vinyl_id/ui/logo.svg`) are © Goobis, all rights reserved. They are not covered by the MIT license, so please don't reuse them without permission. The bundled Google Sans font is under the SIL Open Font License (`app/vinyl_id/ui/fonts/OFL.txt`).

---

<p align="center">
  <a href="LICENSE">MIT</a> · <a href="https://ko-fi.com/goobis2dmx">Support on Ko-fi</a><br><br>
  Vinyl ID v0.9.1 by Goobis<br>
  AI was used to help develop this application
</p>
