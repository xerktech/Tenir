"""Tenir meeting-caption capture client for Windows (XERK-2506).

Capture only: it watches configured meeting windows with Windows Graphics Capture, crops each
frame to the caption box and streams it to Tenir's ``/ws/screen``. All OCR and parsing happen
server-side (docs/meeting-captions.md).
"""

from tenir_capture._version import __version__

__all__ = ["__version__"]
