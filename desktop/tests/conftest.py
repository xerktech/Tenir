from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
# Synthetic Zoom frames from the spike harness (scripts/caption_ocr, XERK-2504).
sys.path.insert(0, str(ROOT / "scripts" / "caption_ocr"))


@pytest.fixture
def zoom_frame():
    """make(caption=True, dark=False) -> a 1920x1080 synthetic Zoom frame."""
    from caption_synth import make_frame

    def make(caption: bool = True, dark: bool = False, seed: int = 7) -> Image.Image:
        return make_frame(random.Random(seed), 0, 1.0, 85, dark=dark, caption=caption).image

    return make
