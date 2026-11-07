"""Turns Home Assistant app options + the Supervisor's MQTT login into env vars.

Run by entrypoint.sh as:  eval "$(python ha_options.py)"
"""

import json
import os
import shlex
import socket
import urllib.request


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    finally:
        s.close()


def mqtt_service() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}
    req = urllib.request.Request("http://supervisor/services/mqtt",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("data", {})


opts = json.load(open(os.environ.get("OPTIONS_FILE", "/data/options.json")))
env = {
    "AUDIO_SOURCE": opts.get("audio_source", "sendspin"),
    "AUDIO_INPUT": opts.get("audio_input") or "",
    "LEARN": str(opts.get("learn", True)).lower(),
    "SIGNAL_THRESHOLD_DB": str(opts.get("signal_threshold_db", -50)),
    "RECEIVER_NAME": opts.get("receiver_name", "Vinyl ID"),
    "DATA_DIR": "/data",
    "WEB_PORT": "8780",
    "PUBLIC_URL": f"http://{lan_ip()}:8780",
    "EDIT_FROM": "172.30.32.2,127.0.0.1",   # edits only through the HA sidebar (ingress)
}
m = mqtt_service()
if m:
    env |= {"MQTT_HOST": m.get("host", ""), "MQTT_PORT": str(m.get("port", 1883)),
            "MQTT_USER": m.get("username", ""), "MQTT_PASSWORD": m.get("password", "")}

for k, v in env.items():
    print(f"export {k}={shlex.quote(v)}")
