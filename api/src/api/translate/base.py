"""Translator seam (XERK-160).

A translator turns one finalized non-English transcript turn into English text.
Implementations are synchronous — the session calls them via ``asyncio.to_thread``
off the caption path, exactly like cue generation — and best-effort: a failed or
empty translation returns ``None`` and the turn simply goes untranslated.
"""

from __future__ import annotations

from typing import Protocol


class Translator(Protocol):
    def translate(self, text: str, *, source_lang: str | None = None) -> str | None:
        """Translate ``text`` (spoken in ``source_lang``, when detected) to English.

        Returns the English translation, or ``None`` when there is nothing to
        emit (empty input, model failure, or output indistinguishable from the
        input). Must not raise for ordinary failures.
        """
        ...
