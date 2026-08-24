from uuid import uuid4

import pytest
from PIL import Image

from floorball_bot.errors import ValidationBlocked
from floorball_bot.media import MediaPipeline


def test_media_pipeline_reencodes_webp_and_strips_metadata(tmp_path):
    source = tmp_path / "photo.jpg"
    image = Image.new("RGB", (1200, 800), "blue")
    exif = Image.Exif()
    exif[0x010E] = "private description"
    image.save(source, exif=exif)

    result = MediaPipeline(tmp_path / "state").process_image(source, uuid4())

    assert result.detected_mime == "image/jpeg"
    assert result.derivative_path.suffix == ".webp"
    with Image.open(result.derivative_path) as derivative:
        assert derivative.getexif() == {}
        assert derivative.width <= 2400
        assert derivative.height <= 2400
    assert source.read_bytes() != result.derivative_path.read_bytes()


def test_media_pipeline_rejects_extension_spoof(tmp_path):
    source = tmp_path / "fake.jpg"
    source.write_text("not an image", encoding="utf-8")
    with pytest.raises(ValidationBlocked):
        MediaPipeline(tmp_path / "state").process_image(source, uuid4())


def test_media_pipeline_rejects_oversized_file_before_decode(tmp_path):
    source = tmp_path / "large.png"
    source.write_bytes(b"x" * 101)
    with pytest.raises(ValidationBlocked, match="size"):
        MediaPipeline(tmp_path / "state", max_input_bytes=100).process_image(source, uuid4())
