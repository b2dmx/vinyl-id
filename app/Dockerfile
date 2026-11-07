FROM python:3.13-slim

# ffmpeg: audio decoding · pulseaudio: virtual "vinyl" speaker both receivers play into
# shairport-sync + avahi: AirPlay receiver · libportaudio2: Sendspin player output
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg pulseaudio pulseaudio-utils libasound2-plugins libportaudio2 \
      shairport-sync avahi-daemon dbus \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vinyl_id ./vinyl_id
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

VOLUME /data
EXPOSE 8095
ENTRYPOINT ["/entrypoint.sh"]
