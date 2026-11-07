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
  eval "$(python /app/vinyl_id/ha_options.py)"
fi
NAME="${RECEIVER_NAME:-Vinyl ID}"

start_sink() {
  pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit \
    --load="module-null-sink sink_name=vinyl sink_properties=device.description=Vinyl"
  sleep 1
  pactl set-default-sink vinyl
  export AUDIO_INPUT=vinyl.monitor AUDIO_INPUT_FORMAT=pulse
}

case "${AUDIO_SOURCE:-sendspin}" in
  sendspin)
    start_sink
    sendspin daemon --name "$NAME" --audio-device pulse --disable-mpris \
      --hardware-volume false --settings-dir /data/sendspin &
    ;;
  airplay)
    start_sink
    mkdir -p /run/dbus
    dbus-daemon --system --fork
    avahi-daemon --daemonize --no-drop-root
    shairport-sync --name="$NAME" --output=pa &
    ;;
  stream) ;;
  *) echo "AUDIO_SOURCE must be sendspin, airplay or stream"; exit 1 ;;
esac

cd /app
exec python -m vinyl_id
