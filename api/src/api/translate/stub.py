"""Model-free translator for CI/dev (XERK-160).

Deterministic so tests and the model-free single-host stack exercise the whole
translation path — session run state → WS messages → persistence → history —
without a GPU. It cannot actually translate; it tags the input and reverses each
word, so the output is recognizably "the translation of X", stable across runs, and
not an echo of X (the session drops a "translation" that repeats its source's words,
XERK-1423).
"""

from __future__ import annotations

import re


class StubTranslator:
    def translate(
        self, text: str, *, source_lang: str | None = None, run_lang: str | None = None
    ) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        lang = source_lang or "auto"
        return f"[{lang}→en] " + re.sub(r"\w+", lambda m: m.group()[::-1], stripped)
