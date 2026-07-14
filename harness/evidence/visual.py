"""Small, deterministic local image descriptors with no network execution."""

from __future__ import annotations

from pathlib import Path

from .schema import VisualDescriptor

MAX_IMAGE_PIXELS = 40_000_000
VISUAL_PROVIDER_ID = "pixel-local-v1"
VISUAL_DESCRIPTOR_CONFIG = {
    "dhash_size": [9, 8],
    "color_size": [4, 4],
    "resampling": "bilinear",
    "score_weights": {"dhash": 0.65, "color": 0.35},
}


class VisualDependencyError(RuntimeError):
    pass


def describe_image(path: str | Path, *, evidence_id: str, asset_id: str) -> VisualDescriptor:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise VisualDependencyError(
            "local image indexing requires the optional Pillow dependency"
        ) from exc

    image_path = Path(path)
    with Image.open(image_path) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("image dimensions exceed the local descriptor safety limit")
        image.load()
        gray = image.convert("L").resize((9, 8), resample=Image.Resampling.BILINEAR)
        pixels = list(_image_data(gray))
        bits = 0
        for row in range(8):
            for column in range(8):
                bits = (bits << 1) | int(pixels[row * 9 + column] > pixels[row * 9 + column + 1])
        rgb = image.convert("RGB").resize((4, 4), resample=Image.Resampling.BILINEAR)
        color_grid = tuple(channel for pixel in _image_data(rgb) for channel in pixel)
    return VisualDescriptor(
        evidence_id=evidence_id,
        asset_id=asset_id,
        image_path=image_path.as_posix(),
        dhash=f"{bits:016x}",
        color_grid=color_grid,
        width=width,
        height=height,
    )


def visual_similarity(left: VisualDescriptor, right: VisualDescriptor) -> float:
    hamming = (int(left.dhash, 16) ^ int(right.dhash, 16)).bit_count()
    hash_score = 1.0 - hamming / 64
    color_distance = sum(abs(a - b) for a, b in zip(left.color_grid, right.color_grid))
    color_score = 1.0 - color_distance / (48 * 255)
    return round(0.65 * hash_score + 0.35 * color_score, 10)


def _image_data(image):
    getter = getattr(image, "get_flattened_data", None)
    return getter() if getter is not None else image.getdata()


def visual_provider_metadata() -> dict[str, object]:
    try:
        from PIL import __version__ as pillow_version
    except ImportError:
        pillow_version = None
    return {
        "id": VISUAL_PROVIDER_ID,
        "algorithm_version": 1,
        "config": VISUAL_DESCRIPTOR_CONFIG,
        "pillow_version": pillow_version,
    }
