"""Evidence retrieval seam for grounded cues (XERK-120).

Before the cue model runs, the session gathers *evidence* — short, dated
snippets from live sources (the RSS news corpus, Wikipedia, SearXNG web
search) — and rides it in the model's prompt. The cue model's weights are
frozen years in the past (the deployed Qwen3 reports an October 2023 cutoff),
so anything about current events answered from memory is a guess; evidence
makes the cue's facts come from sources that are right *now*, and lets the cue
carry an attribution the listener can trust.

The seam mirrors the ``CueGenerator``/``Transcriber`` seams: retrieval knows
nothing about the WebSocket or the session's timing rules, and the stub swap
keeps the whole grounded path testable without a network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Evidence:
    """One retrieved snippet, prompt-ready.

    ``source`` is the short label shown to both the model and (if the cue cites
    this evidence) the listener — "BBC News", "Wikipedia", a web domain.
    ``published`` is a human-readable date string when the source carries one;
    it rides the prompt so the model can weigh freshness.
    """

    source: str
    title: str
    snippet: str
    published: str | None = None
    url: str | None = None


class EvidenceRetriever(Protocol):
    async def retrieve(self, turns: Sequence[str]) -> list[Evidence]:
        """Evidence for the current conversation window, best-effort: whatever
        the tiers produced inside the retriever's deadline, freshest-and-most-
        relevant first, already trimmed to the prompt budget. Empty on any
        failure — a cue without evidence still beats no cue."""
        ...

    async def close(self) -> None:
        """Release network resources at session end."""
        ...
