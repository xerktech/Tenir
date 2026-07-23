"""LocalAgreement-2 incremental commit for live captions (XERK-90).

The realtime windowing in :mod:`api.stt.streaming` re-transcribes an overlapping
*trailing* audio window on a fixed cadence. Emitting each fresh hypothesis
verbatim makes the on-screen caption rewrite words that were already shown — the
"constantly changing and adjusting until it drops a final guess" behaviour we want
to kill. It doesn't read as live captioning.

``LocalAgreement`` applies the **LocalAgreement-2** policy (Macháček, Dabre &
Bojar, *"Turning Whisper into Real-Time Transcription System"*, 2023): a word is
*committed* — shown as stable and never rewritten — only once two consecutive
hypotheses agree on it. Everything after the agreed prefix stays *tentative* (a
word or two behind real time) until a later hypothesis confirms it. The caption
then grows word by word instead of flickering, which is exactly the "accurate
word by word, only a couple words behind" feel the ticket asks for.

The policy is text-only (no timestamps), so it works with any engine — including
Voxtral, whose vLLM ``/audio/transcriptions`` endpoint returns no per-word timing.
"""

from __future__ import annotations


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    """Length of the longest shared prefix of two word lists."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class LocalAgreement:
    """Commits words that two consecutive window hypotheses agree on.

    Feed each new *full-window* hypothesis (already tokenised into words) to
    :meth:`commit`. :attr:`committed` holds the stable prefix that has been shown
    and won't change; :attr:`tentative` holds the not-yet-confirmed tail. Because
    successive decode windows overlap, a fresh hypothesis re-states words that are
    already committed; :meth:`commit` strips that overlap first, so committed words
    are never re-emitted or reconsidered.
    """

    # How many trailing committed words to search when re-anchoring an overlapping
    # window. The trailing decode window is only a few seconds, so its leading words
    # overlap just the most-recently committed handful — bounding the search keeps a
    # long transcript from being re-scanned every cadence and avoids matching a much
    # older, coincidental repetition of the same word further back.
    _MAX_OVERLAP = 16

    def __init__(self) -> None:
        self._committed: list[str] = []
        self._pending: list[str] = []  # previous round's post-overlap tail

    def commit(self, hypothesis: list[str]) -> None:
        """Ingest a new window hypothesis and extend the committed prefix.

        Any leading words that merely re-state what's already committed are dropped;
        of the genuinely new words, those that match last round's tail (the
        LocalAgreement-2 agreement) are committed and the rest are held as the new
        tentative tail.
        """
        fresh = self._strip_committed(hypothesis)
        agreed = _common_prefix_len(self._pending, fresh)
        if agreed:
            self._committed.extend(fresh[:agreed])
        self._pending = fresh[agreed:]

    def _strip_committed(self, hyp: list[str]) -> list[str]:
        """Return only the part of ``hyp`` beyond what's already committed.

        The trailing decode window overlaps the end of the committed transcript, so
        a fresh hypothesis begins by repeating some committed words. Find the longest
        overlap (a committed suffix equal to a ``hyp`` prefix) and drop it; if the
        window has slid far enough that no overlap remains, ``hyp`` is all new.
        """
        if not self._committed or not hyp:
            return list(hyp)
        tail = self._committed[-self._MAX_OVERLAP :]
        max_k = min(len(tail), len(hyp))
        for k in range(max_k, 0, -1):
            if tail[-k:] == hyp[:k]:
                return list(hyp[k:])
        return list(hyp)

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
