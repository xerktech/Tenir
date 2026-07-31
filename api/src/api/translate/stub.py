"""Model-free translator for CI/dev (XERK-160).

Deterministic so tests and the model-free single-host stack exercise the whole
translation path — session run state → WS messages → persistence → history —
without a GPU. It cannot actually translate; it wraps the input so the output is
recognizably "the translation of X" and stable across runs.
"""

from __future__ import annotations


class StubTranslator:
    def translate(self, text: str, *, source_lang: str | None = None) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        lang = source_lang or "auto"
        return f"[{lang}→en] {stripped}"
