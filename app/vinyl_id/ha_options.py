"""Turns Home Assistant app options + the Supervisor's MQTT login into env vars.

Run by entrypoint.sh as:  eval "$(python ha_options.py)"
"""

import json
import os
import shlex
import urllib.request


def mqtt_service() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}
    req = urllib.request.Request("http://supervisor/services/mqtt",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("data", {})
    except (OSError, ValueError):     # no broker installed (the Supervisor answers with an error): do without
        return {}


opts = json.load(open(os.environ.get("OPTIONS_FILE", "/data/options.json")))
env = {
    "AUDIO_SOURCE": opts.get("audio_source", "sendspin"),
    "AUDIO_INPUT": opts.get("audio_input") or "",
    "LEARN": str(opts.get("learn", True)).lower(),
    "SIGNAL_THRESHOLD_DB": str(opts.get("signal_threshold_db", -50)),
    "RECEIVER_NAME": opts.get("receiver_name") or "Vinyl ID Listener",
    "DATA_DIR": "/data",
    "WEB_PORT": "8780",
    "MAX_LIBRARY_MB": str(opts.get("max_library_mb") or 0),
    # edits only through the HA sidebar (ingress), plus any addresses you allow yourself
    "EDIT_FROM": ",".join(["172.30.32.2", "127.0.0.1", *(opts.get("edit_from") or "").replace(" ", "").split(",")]).strip(","),
}
m = mqtt_service()
if m:
    env |= {"MQTT_HOST": m.get("host", ""), "MQTT_PORT": str(m.get("port", 1883)),
            "MQTT_USER": m.get("username", ""), "MQTT_PASSWORD": m.get("password", "")}

for k, v in env.items():
    print(f"export {k}={shlex.quote(v)}")
