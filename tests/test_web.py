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
