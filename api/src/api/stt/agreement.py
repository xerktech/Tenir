"""LocalAgreement-2 incremental commit for live captions (XERK-90).

The realtime windowing in :mod:`api.stt.streaming` re-transcribes the in-flight
segment on a fixed cadence. Emitting each fresh hypothesis verbatim makes the
on-screen caption rewrite words that were already shown — the "constantly changing
and adjusting until it drops a final guess" behaviour we want to kill. It doesn't
read as live captioning.

``LocalAgreement`` applies the **LocalAgreement-2** policy (Macháček, Dabre &
Bojar, *"Turning Whisper into Real-Time Transcription System"*, 2023): a word is
*committed* — shown as stable and never rewritten — only once two consecutive
hypotheses agree on it. Everything after the agreed prefix stays *tentative* (a
word or two behind real time) until a later hypothesis confirms it. The caption
then grows word by word instead of flickering, which is the "accurate word by
word, only a couple words behind" feel the ticket asks for.

Two properties make this robust against real engine output (learned the hard way —
the first cut wasn't):

- **Anchored hypotheses.** Each hypothesis must be a decode of the *whole* in-flight
  segment (same audio start every cadence), so successive hypotheses share a stable
  prefix and the agreement is a plain longest-common-prefix. A *sliding* decode
  window starts at a different point each pass, so its hypotheses never line up and
  nothing ever commits — the streaming layer decodes the whole segment for partials
  when LocalAgreement is on for exactly this reason.
- **Normalized comparison.** Voxtral re-punctuates and re-capitalizes the window
  every pass (``So`` → ``So,``, ``the`` → ``The``). Comparing raw tokens would break
  the prefix on the first cosmetic change and commit almost nothing, so agreement is
  checked on a lowercased, punctuation-stripped form while the original surface form
  is what gets displayed.

The policy is text-only (no timestamps), so it works with any engine — including
Voxtral, whose vLLM ``/audio/transcriptions`` endpoint returns no per-word timing.
"""

from __future__ import annotations

# Punctuation stripped from a token's edges before comparing two hypotheses, so a
# word that only gained/lost a comma or a capital between passes still counts as
# agreement. Interior marks (the apostrophe in "don't") are left alone.
_EDGE_PUNCT = "\"'“”‘’.,!?;:…()[]{}«»—–-·"


def _norm(token: str) -> str:
    """Fold a token to the form used for agreement: edge punctuation off, lowercased."""
    return token.strip(_EDGE_PUNCT).lower()


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    """Length of the longest shared prefix of two (normalized) word lists."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class LocalAgreement:
    """Commits words that two consecutive whole-segment hypotheses agree on.

    Feed each new *whole-segment* hypothesis (already tokenised into words) to
    :meth:`commit`. :attr:`committed` holds the stable prefix that has been shown and
    won't change; :attr:`tentative` holds the not-yet-confirmed tail. Committed words
    keep the surface form they were frozen with, so later cosmetic re-spellings of the
    same word never rewrite what's on screen.
    """

    def __init__(self) -> None:
        self._committed: list[str] = []  # frozen display tokens — never rewritten
        self._pending: list[str] = []  # tentative display tail
        self._prev_norm: list[str] = []  # previous whole hypothesis, normalized

    def commit(self, hypothesis: list[str]) -> None:
        """Ingest a new whole-segment hypothesis and extend the committed prefix.

        Commits up to the longest prefix this hypothesis shares with the previous one
        (LocalAgreement-2), holding everything past it as the tentative tail. The
        committed prefix only ever grows: if a later hypothesis disagrees about a word
        that was already committed, the commit stands and the screen doesn't flicker.
        """
        norm = [_norm(t) for t in hypothesis]
        agreed = _common_prefix_len(self._prev_norm, norm)
        n = len(self._committed)
        # Never un-commit (>= n) and never run past this hypothesis (<= len).
        new_n = max(n, min(agreed, len(hypothesis)))
        if new_n > n:
            self._committed.extend(hypothesis[n:new_n])
        self._pending = hypothesis[new_n:]
        self._prev_norm = norm

    @property
    def committed(self) -> list[str]:
        """The stable words shown so far — never rewritten."""
        return self._committed

    @property
    def tentative(self) -> list[str]:
        """The unconfirmed tail that may still change on the next hypothesis."""
        return self._pending

    def committed_text(self) -> str:
        return " ".join(self._committed)

    def caption_text(self) -> str:
        """The full live caption: committed prefix plus the tentative tail."""
        return " ".join(self._committed + self._pending)
