"""Embedded WebP thumbnails."""

from __future__ import annotations

import base64
import io
import random

import pytest
from PIL import Image

from instagram_mcp.thumbnails import ThumbnailError, webp_data_uri


def jpeg(size: tuple[int, int], *, noisy: bool = False) -> bytes:
    if noisy:  # worst case for compression: random pixels
        rng = random.Random(7)
        image = Image.frombytes("RGB", size, bytes(rng.randrange(256) for _ in range(size[0] * size[1] * 3)))
    else:
        image = Image.linear_gradient("L").resize(size).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def decode(uri: str) -> tuple[Image.Image, int]:
    assert uri.startswith("data:image/webp;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)), len(raw)


@pytest.mark.parametrize(("size", "expected"), [((1080, 1920), (90, 160)), ((1080, 1080), (160, 160))])
def test_long_side_is_160_px(size: tuple[int, int], expected: tuple[int, int]) -> None:
    image, _ = decode(webp_data_uri(jpeg(size)))
    assert image.format == "WEBP" and image.size == expected


def test_stays_under_the_size_budget_even_for_noise() -> None:
    image, size = decode(webp_data_uri(jpeg((640, 640), noisy=True)))
    assert size <= 15_000 and image.size == (160, 160)
    with pytest.raises(ThumbnailError, match="above 500 bytes"):
        webp_data_uri(jpeg((640, 640), noisy=True), max_bytes=500)


def test_small_images_are_not_enlarged() -> None:
    image, _ = decode(webp_data_uri(jpeg((100, 60))))
    assert image.size == (100, 60)


def test_unreadable_bytes() -> None:
    with pytest.raises(ThumbnailError, match="Unreadable image"):
        webp_data_uri(b"<html>not an image</html>")
