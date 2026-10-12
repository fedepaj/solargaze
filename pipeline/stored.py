"""
How a byte raster is stored: one channel, byte 0 for no data, in one of two
ways, and never as a one-channel PNG.

``lossy`` (AVIF) is for a level that is modelled to a few decibels or
measured to a degree, which does not need every half-decibel step kept. It
is stored the way a video frame is, transformed and quantised: at QUALITY
the heat months come back within 0.2 °C rms and the noise within 0.3 dB
rms, in about 37 % of the bytes of the PNG (measured on eleven published
tiles, from the lightest to the heaviest). Prediction between the months,
the other half of a video codec, was measured too and gives about 15 %
more: not worth a video decoder in the page. The image has two planes: the
level, with the cells that have none filled from their nearest neighbour so
that a building's edge is not a cliff the codec spends bits on, and the
alpha, lossless, 0 where there is no data. Read back, a cell with data is
never byte 0, whatever the codec made of it.

``exact`` (lossless WebP) is for what must come back as written: heights
compared with a threshold, epochs stored as differences, a model's ratio.
Between 68 % and 86 % of the PNG.

Not PNG, because Safari's createImageBitmap gives back a one-channel PNG
with one cell in four a byte or two off and no-data cells with a value
(measured on Safari 26.5); WebP and AVIF come back exact there.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

QUALITY = 90
SPEED = 4
# What a product's `encoding` says of files written lossy (the app reads it).
LOSSY = {"codec": "avif", "quality": QUALITY, "nodata": "alpha"}


def encode_lossy(byte: np.ndarray) -> bytes:
    """A byte image, 0 for no data, as AVIF. Raises if the mask does not come back exact."""
    from scipy import ndimage
    valid = byte > 0
    level = byte
    if valid.any() and not valid.all():
        level = byte[tuple(ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True))]
    la = np.dstack([level, np.where(valid, 255, 0).astype(np.uint8)])
    out = io.BytesIO()
    Image.fromarray(la, "LA").save(out, "AVIF", quality=QUALITY, speed=SPEED, subsampling="4:0:0",
                                   advanced={"alpha:cq-level": "0"})
    data = out.getvalue()
    if not np.array_equal(decode(data, ".avif") > 0, valid):
        raise ValueError("the no-data mask did not survive the encoding")
    return data


def encode_exact(byte: np.ndarray) -> bytes:
    """A byte image as lossless WebP. Raises if it does not come back as it was."""
    out = io.BytesIO()
    Image.fromarray(byte, "L").save(out, "WEBP", lossless=True, quality=100, method=4)
    data = out.getvalue()
    if not np.array_equal(decode(data, ".webp"), byte):
        raise ValueError("the image did not survive the encoding")
    return data


def decode(data: bytes, suffix: str) -> np.ndarray:
    """The bytes back, 0 for no data: what the app reads (channels in js/atmo/tiles.js)."""
    img = Image.open(io.BytesIO(data))
    if suffix != ".avif":
        return np.asarray(img.convert("L"))
    rgba = np.asarray(img.convert("RGBA"))
    return np.where(rgba[..., 3] > 127, np.maximum(rgba[..., 0], 1), 0).astype(np.uint8)


def read(path: Path) -> np.ndarray:
    path = Path(path)
    return decode(path.read_bytes(), path.suffix)


def lossy(byte: np.ndarray, path: Path) -> None:
    Path(path).write_bytes(encode_lossy(np.asarray(byte)))


def exact(byte: np.ndarray, path: Path) -> None:
    Path(path).write_bytes(encode_exact(np.asarray(byte)))
