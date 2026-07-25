"""Evidence retrieval seam: "off" (no grounding), "stub" (deterministic) or
"live" (news corpus + Wikipedia + SearXNG). See base.py for the why."""

from __future__ import annotations

from api.config import settings
from api.cue.retrieval.base import Evidence, EvidenceRetriever
from api.cue.retrieval.query import RetrievalQuery, build_query

__all__ = [
    "Evidence",
    "EvidenceRetriever",
    "RetrievalQuery",
    "build_query",
    "make_evidence_retriever",
]


def make_evidence_retriever() -> EvidenceRetriever | None:
    """Factory selected by ``API_CUE_RETRIEVAL_BACKEND``. ``None`` when retrieval
    is off — the session then calls the cue generator ungrounded, exactly the
    pre-XERK-120 behaviour."""
    backend = settings.cue_retrieval_backend

    if backend == "off":
        return None

    if backend == "stub":
        from api.cue.retrieval.stub import StubEvidenceRetriever

        return StubEvidenceRetriever()

    if backend == "live":
        # Imported lazily so httpx/the news store load only when grounding is on.
        from api.cue.retrieval.live import LiveEvidenceRetriever
        from api.persistence.news import get_news_store

        return LiveEvidenceRetriever(
            news_store=get_news_store(),
            wikipedia_endpoint=settings.cue_wikipedia_endpoint,
            kiwix_endpoint=settings.cue_kiwix_endpoint,
            kiwix_book=settings.cue_kiwix_book,
            searxng_endpoint=settings.cue_searxng_endpoint,
            searxng_engines=settings.cue_searxng_engines,
            deadline_ms=settings.cue_retrieval_deadline_ms,
            max_evidence=settings.cue_retrieval_max_evidence,
        )
    raise ValueError(
        f"unknown cue retrieval backend: {backend!r} (expected 'off', 'stub' or 'live')"
    )
