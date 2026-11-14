"""Meeting captions read off captured screen frames (XERK-2505, docs/meeting-captions.md).

The desktop capture client streams caption-box crops and periodic full window frames over
``/ws/screen``; ``ocr`` reads their text through the tenir-ocr service, ``zoom`` or ``teams``
(by the frame's platform) turns that text into participant names and speaker-attributed
caption lines, ``merge`` folds the rolling captions into final lines, and ``meetings`` opens and closes the conversations.
Frames are never stored: only the text derived from them reaches persistence.
"""

import unicodedata


def has_control_chars(text: str) -> bool:
    """Control (Cc) or format (Cf) characters: no org site key or name holds one, and a NUL
    500s Postgres. Shared by /ws/screen (refuses the frame) and the history org filter, so a
    meeting stored from a frame can always be selected by its key (XERK-2881)."""
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text)
