#!/bin/sh
# Vinyl ID start-up. Works as a Home Assistant app (reads /data/options.json and
# the MQTT login from the Supervisor) or as a plain Docker container (env vars).
#
# AUDIO_SOURCE picks how the record arrives:
#   sendspin → Vinyl ID appears in Music Assistant as a Sendspin player
#   airplay  → Vinyl ID appears in Music Assistant as an AirPlay speaker
#   stream   → no receiver; AUDIO_INPUT is read directly (URL, device or file)
# Both receivers play into a silent virtual speaker ("vinyl") that Vinyl ID listens to.
set -e

if [ -f /data/options.json ]; then
  ha_env="$(python /app/vinyl_id/ha_options.py)"   # on its own line so a failure stops the start
  eval "$ha_env"
fi
NAME="${RECEIVER_NAME:-Vinyl ID Listener}"

# Run a receiver in the background and start it again if it ever exits,
# so Vinyl ID is never left listening to a speaker nothing plays into.
keep_running() {
  ( while true; do
      "$@" || true
      echo "$1 stopped; starting it again in 5 s"
      sleep 5
    done ) &
}

start_sink() {
  pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit \
    --load="module-null-sink sink_name=vinyl sink_properties=device.description=Vinyl"
  for _ in 1 2 3 4 5 6 7 8 9 10; do   # wait until it answers (up to 5 s)
    pactl info >/dev/null 2>&1 && break
    sleep 0.5
  done
  pactl set-default-sink vinyl
  export AUDIO_INPUT=vinyl.monitor AUDIO_INPUT_FORMAT=pulse
}

case "${AUDIO_SOURCE:-sendspin}" in
  sendspin)
    start_sink
    keep_running sendspin daemon --name "$NAME" --audio-device pulse --disable-mpris \
      --hardware-volume false --settings-dir /data/sendspin
    ;;
  airplay)
    start_sink
    mkdir -p /run/dbus
    dbus-daemon --system --fork
    avahi-daemon --daemonize --no-drop-root
    keep_running shairport-sync --name="$NAME" --output=pa
    ;;
  stream) ;;
  *) echo "AUDIO_SOURCE must be sendspin, airplay or stream"; exit 1 ;;
esac

cd /app
exec python -m vinyl_id
