from vinyl_id.web import is_image


def test_is_image_accepts_real_formats():
    assert is_image(b"\xff\xd8\xff\xe0" + b"0" * 20)
    assert is_image(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    assert is_image(b"GIF89a" + b"0" * 20)
    assert is_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")


def test_is_image_rejects_everything_else():
    assert not is_image(None)
    assert not is_image(b"")
    assert not is_image(b'{"data": {"token": "x"}}')
    assert not is_image(b"<html></html>")


def test_seconds_reads_both_forms():
    from vinyl_id.web import _seconds
    assert _seconds("2:01") == 121
    assert _seconds("0:09") == 9
    assert _seconds(41) == 41
    for bad in ("", "x", "-1", 0, True, "99:00"):
        try:
            _seconds(bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError(bad)


def test_icy_block_is_padded_to_16_bytes():
    from vinyl_id.stream import icy_block
    b = icy_block("Artist - Song", "http://x/art")
    assert len(b) == 1 + b[0] * 16
    assert b[1:].rstrip(b"\0") == b"StreamTitle='Artist - Song';StreamUrl='http://x/art';"


def test_the_version_is_the_same_everywhere():
    import json
    import pathlib
    import re

    from vinyl_id.config import VERSION

    root = pathlib.Path(__file__).parent.parent
    assert f'version: "{VERSION}"' in (root / "app/config.yaml").read_text(encoding="utf-8")
    assert f"Vinyl ID v{VERSION} by Goobis" in (root / "README.md").read_text(encoding="utf-8")
    comp = root / "custom_components/vinyl_id"
    assert json.loads((comp / "manifest.json").read_text(encoding="utf-8"))["version"] == VERSION
    assert re.findall(r"Vinyl ID v([\d.]+\d) by Goobis", (comp / "translations/en.json").read_text(encoding="utf-8")) == [VERSION]


def test_the_app_starts_without_an_mqtt_broker(tmp_path):
    """The options script must not fail when the Supervisor has no MQTT service to offer."""
    import os
    import pathlib
    import subprocess
    import sys

    options = tmp_path / "options.json"
    options.write_text("{}")
    script = pathlib.Path(__file__).parent.parent / "app/vinyl_id/ha_options.py"
    env = {**os.environ, "OPTIONS_FILE": str(options), "SUPERVISOR_TOKEN": "x"}   # "supervisor" cannot be reached here
    out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "MQTT_HOST" not in out.stdout and "export RECEIVER_NAME='Vinyl ID Listener'" in out.stdout


def test_mqtt_entities_only_when_asked_for(tmp_path):
    """The integration shows the record; the broker is used only when 'Also publish MQTT entities' is on."""
    import http.server
    import json
    import os
    import pathlib
    import subprocess
    import sys
    import threading

    class Supervisor(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": {"host": "broker", "port": 1883, "username": "u", "password": "p"}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Supervisor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = pathlib.Path(__file__).parent.parent / "app/vinyl_id/ha_options.py"
    env = {**os.environ, "SUPERVISOR_TOKEN": "x", "SUPERVISOR_URL": f"http://127.0.0.1:{server.server_port}"}

    def run(opts):
        options = tmp_path / "options.json"
        options.write_text(json.dumps(opts))
        out = subprocess.run([sys.executable, str(script)], env={**env, "OPTIONS_FILE": str(options)},
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return out.stdout

    try:
        assert "MQTT_HOST" not in run({})
        assert "MQTT_HOST" not in run({"mqtt_entities": False})
        assert "export MQTT_HOST=broker" in run({"mqtt_entities": True})
    finally:
        server.shutdown()


def test_settings_left_empty_and_settings_that_are_no_number(monkeypatch):
    import pytest

    from vinyl_id import config

    monkeypatch.setenv("MAX_LIBRARY_MB", "")
    monkeypatch.setenv("MQTT_PORT", "  ")
    assert config._f("MAX_LIBRARY_MB", 0) == 0 and config._f("MQTT_PORT", 1883) == 1883
    monkeypatch.setenv("MQTT_PORT", "8883")
    assert config._f("MQTT_PORT", 1883) == 8883
    monkeypatch.setenv("STOP_SECONDS", "forty")
    with pytest.raises(SystemExit, match="STOP_SECONDS"):
        config._f("STOP_SECONDS", 45)


def test_the_address_for_displays_follows_the_network(monkeypatch):
    from vinyl_id import config

    found = iter(["127.0.0.1", "192.0.2.7", "192.0.2.8"])     # no network yet at start-up, then one
    monkeypatch.setattr(config, "lan_ip", lambda: next(found))
    monkeypatch.setattr(config, "_found", (0.0, ""))
    clock = [1000.0]
    monkeypatch.setattr(config.time, "monotonic", lambda: clock[0])
    s = config.Settings(web_port=8780)
    assert s.public_url == "http://127.0.0.1:8780"
    clock[0] += 10
    assert s.public_url == "http://192.0.2.7:8780"
    clock[0] += 10
    assert s.public_url == "http://192.0.2.7:8780"            # not looked up on every use
    clock[0] += 100
    assert s.public_url == "http://192.0.2.8:8780"
    assert config.Settings(public_url_set="http://vinyl.example").public_url == "http://vinyl.example"


def test_edits_can_be_allowed_for_a_range_of_addresses():
    import ipaddress

    from vinyl_id.web import networks

    nets = networks("192.168.1.20, 10.0.0.0/8, nonsense,")
    assert len(nets) == 2
    assert any(ipaddress.ip_address("10.4.5.6") in n for n in nets)
    assert not any(ipaddress.ip_address("192.168.1.21") in n for n in nets)
