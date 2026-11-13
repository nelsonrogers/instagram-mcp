"""Small embedded post thumbnails (``data:image/webp;base64,…``).

Instagram's CDN links expire after a few days, and chat sandboxes often can't download anything,
so the output embeds the image itself: about 160 px on the long side, WebP, well under 15 KB.
"""

from __future__ import annotations

import base64
import io
from typing import Any

MAX_SIDE = 160
MAX_BYTES = 15_000
QUALITIES = (60, 45, 30, 15)


class ThumbnailError(ValueError):
    """The image could not be turned into a thumbnail."""


def webp_data_uri(image_bytes: bytes, *, max_side: int = MAX_SIDE, max_bytes: int = MAX_BYTES) -> str:
    """Resize an image (JPEG, PNG, WebP, …) and return it as a WebP data URI."""
    from PIL import Image, UnidentifiedImageError  # imported lazily: keeps server start-up fast

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image: Any = source.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ThumbnailError(f"Unreadable image ({type(exc).__name__}).") from exc
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    encoded = b""
    for quality in QUALITIES:
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=quality, method=6)
        encoded = buffer.getvalue()
        if len(encoded) <= max_bytes:
            break
    else:
        raise ThumbnailError(f"The thumbnail stays above {max_bytes} bytes even at low quality.")
    return "data:image/webp;base64," + base64.b64encode(encoded).decode("ascii")
