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
