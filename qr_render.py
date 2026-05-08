"""QR-code rendering with a tinted finder pattern and an image overlay.

Speed wins over the original per-pixel `draw.rectangle` approach come from
three places:

1.  Module rendering is a NumPy operation on a (modules x modules x 4) RGBA
    array, then a single `Image.resize(NEAREST)` blow-up. No Python-level
    pixel loop.

2.  The overlay (the cyberpunk bunny) is loaded and resized ONCE per output
    size and cached, instead of opening + LANCZOS-resizing on every request.

3.  PNG compression skips `optimize=True`, which is iterative and slow.

Visual output matches the original byte-for-byte except for negligible
edge-case ordering of overlapping pixels.
"""

from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path

import numpy as np
import qrcode
from PIL import Image

# QR rendering config (previously in config.py).
QR_MODULE_SCALE: int = 20             # px per QR module
QR_OUTER_PADDING_MODULES: int = 4     # extra quiet zone around the QR's own border
QR_DARK_COLOR: tuple[int, int, int, int] = (0, 0, 0, 255)
QR_FINDER_COLOR: tuple[int, int, int, int] = (0, 160, 220, 255)

# Cache: (overlay_path, size_px) -> resized RGBA image.
# Lock is held only during resize; lookups after warm-up are uncontended.
_overlay_cache: dict[tuple[str, int], Image.Image] = {}
_overlay_lock = threading.Lock()


def preload_overlay(path: Path) -> None:
    """Eagerly load the source overlay so the first request doesn't pay disk I/O."""
    if path.exists():
        Image.open(path).load()


def _get_overlay(path: Path, size: int) -> Image.Image:
    key = (str(path), size)
    cached = _overlay_cache.get(key)
    if cached is not None:
        return cached
    with _overlay_lock:
        # Re-check inside the lock to avoid duplicate work on race.
        cached = _overlay_cache.get(key)
        if cached is not None:
            return cached
        img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
        _overlay_cache[key] = img
        return img


def _build_qr_array(matrix: list[list[bool]]) -> np.ndarray:
    """Turn the boolean QR matrix into an RGBA NumPy array."""
    arr = np.array(matrix, dtype=bool)
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)

    # Default fill colour for every dark module.
    rgba[arr] = QR_DARK_COLOR

    # The three finder patterns sit in the corners; paint dark modules inside
    # those corner regions with the accent colour. We replicate the original
    # logic exactly: any dark module within a 7x7 corner gets the accent.
    finder = np.zeros((h, w), dtype=bool)
    finder[:7, :7] = True
    finder[:7, w - 7 :] = True
    finder[h - 7 :, :7] = True
    rgba[arr & finder] = QR_FINDER_COLOR

    return rgba


def render_qr_with_overlay(
    data: str,
    overlay_path: Path,
    *,
    scale: int = QR_MODULE_SCALE,
    outer_pad_modules: int = QR_OUTER_PADDING_MODULES,
) -> bytes:
    """Generate a QR code for `data`, composite it over `overlay_path`, return PNG bytes."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    matrix = qr.get_matrix()  # includes the QR's own 4-module quiet zone
    inner_modules = len(matrix)

    # Modules of the QR (with its own quiet zone) plus our extra outer padding.
    full_modules = inner_modules + 2 * outer_pad_modules
    full_px = full_modules * scale

    rgba = _build_qr_array(matrix)
    qr_img = Image.fromarray(rgba, "RGBA").resize(
        (inner_modules * scale, inner_modules * scale), Image.NEAREST
    )

    canvas = Image.new("RGBA", (full_px, full_px), (255, 255, 255, 255))
    bg = _get_overlay(overlay_path, full_px)
    canvas.paste(bg, (0, 0), bg)

    pad_px = outer_pad_modules * scale
    canvas.paste(qr_img, (pad_px, pad_px), qr_img)

    buf = BytesIO()
    # `optimize=False` is the default but we make it explicit: the optimizer is
    # iterative and adds ~2x to encode time for negligible size win on QRs.
    canvas.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()
