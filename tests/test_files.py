import io

from PIL import Image

from memorybot.files import prepare_file


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (255, 0, 0, 128)).save(out, "PNG")
    return out.getvalue()


def test_photos_are_shrunk_to_jpeg():
    data, ext = prepare_file(_png(4000, 3000), "IMG_1234.PNG", "image/png", max_px=2048)
    assert ext == ".jpg"
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"
        assert img.size == (2048, 1536)


def test_small_photos_keep_size():
    data, _ = prepare_file(_png(800, 600), "a.png", "image/png")
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (800, 600)


def test_pdf_is_stored_unchanged():
    assert prepare_file(b"%PDF-1.4", "Manual.PDF", "application/pdf") == (b"%PDF-1.4", ".pdf")


def test_broken_image_is_stored_as_is():
    assert prepare_file(b"not an image", "x.heic", "image/heic") == (b"not an image", ".heic")


def test_unknown_extension():
    assert prepare_file(b"x", "noext", "application/octet-stream") == (b"x", ".bin")
