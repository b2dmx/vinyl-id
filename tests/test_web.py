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
