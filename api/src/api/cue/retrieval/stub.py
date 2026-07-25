"""Model-free, network-free evidence retriever for CI/dev (XERK-120).

Deterministic so tests (and the model-free single-host stack) exercise the
whole grounded-cue path — retrieval → evidence in the generator prompt →
source label on the WS frame → persistence → history — without a network.
Mirrors the role of ``StubCueGenerator``.
"""

from __future__ import annotations

from collections.abc import Sequence

from api.cue.retrieval.base import Evidence
from api.cue.retrieval.query import build_query


class StubEvidenceRetriever:
    async def retrieve(self, turns: Sequence[str]) -> list[Evidence]:
        query = build_query(turns)
        if not query:
            return []
        # One deterministic snippet echoing the top keyword, so tests can assert
        # both the evidence flow and the attribution that rides out of it.
        keyword = query.keywords[0]
        return [
            Evidence(
                source="Stub Reference",
                title=f"About {keyword}",
                snippet=f"Verified stub context about {keyword}.",
                published="2026-01-01",
            )
        ]

    async def close(self) -> None:
        return None
