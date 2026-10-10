"""Retrieval agent: cited policy sections from the Markdown knowledge base.

Reuses the RAG engine's building blocks: ``chunk_markdown_by_heading`` for
section-level chunks with ``file.md#slug`` citations, ``BM25Search`` for
lexical search, bge-small embeddings for dense search, and
``reciprocal_rank_fusion`` to merge them. The KB is ~25 sections, so the whole
index lives in memory and is rebuilt in a second at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from app.ingestion.chunking import chunk_markdown_by_heading
from app.retriever.bm25_search import BM25Search, reciprocal_rank_fusion
from app.utils.logging import get_logger

logger = get_logger(__name__)

# The file preamble (Source ID / Applies to) and the SOP's company background state
# no rule, so they are never a valid citation for a policy answer.
NON_POLICY_SECTIONS = {"overview", "company-context"}


@dataclass
class RetrievedSection:
    citation: str
    section: str
    text: str
    score: float


class KnowledgeBase:
    """Section-level hybrid index over ``knowledge_base/*.md``."""

    def __init__(self, kb_dir: str | Path, use_embeddings: bool = True) -> None:
        self.kb_dir = Path(kb_dir)
        self.sections: list[Document] = []
        for path in sorted(self.kb_dir.glob("*.md")):
            for doc in chunk_markdown_by_heading(path.read_text(encoding="utf-8"), path.name):
                if doc.metadata["anchor"] not in NON_POLICY_SECTIONS:
                    self.sections.append(doc)
        if not self.sections:
            raise FileNotFoundError(f"No Markdown files found in {self.kb_dir}")

        self._by_citation = {d.metadata["citation"]: d for d in self.sections}
        self._bm25 = BM25Search()
        self._bm25.build_index(self.sections)

        self._embeddings = None
        self._vectors = None
        if use_embeddings:
            try:
                import numpy as np

                from app.embeddings.factory import get_embeddings_model

                self._embeddings = get_embeddings_model()
                self._vectors = np.array(
                    self._embeddings.embed_documents([d.page_content for d in self.sections])
                )
            except Exception as exc:  # model download failure, no torch, ...
                logger.warning("copilot_embeddings_unavailable", error=str(exc))
                self._embeddings = None
        logger.info("copilot_kb_loaded", sections=len(self.sections), dense=self._vectors is not None)

    @property
    def dense_enabled(self) -> bool:
        return self._vectors is not None

    @property
    def embeddings(self) -> Any:
        """The loaded embedding model, or None when dense search is off."""
        return self._embeddings if self.dense_enabled else None

    def get(self, citation: str) -> Document | None:
        return self._by_citation.get(citation)

    def _dense(self, query: str, k: int) -> list[tuple[Document, float]]:
        if self._embeddings is None or self._vectors is None:
            return []
        import numpy as np

        q = np.array(self._embeddings.embed_query(query))
        scores = self._vectors @ q  # vectors are normalised, so this is cosine
        order = np.argsort(-scores)[:k]
        return [(self.sections[i], float(scores[i])) for i in order]

    def search(self, query: str, k: int = 4) -> list[RetrievedSection]:
        """Hybrid BM25 + dense search fused with RRF. Scores are RRF scores."""
        lexical = self._bm25.search(query, k=10)
        dense = self._dense(query, k=10)
        fused = reciprocal_rank_fusion([lst for lst in (lexical, dense) if lst])
        return [
            RetrievedSection(
                citation=d.metadata["citation"],
                section=d.metadata["section"],
                text=d.page_content,
                score=round(score, 4),
            )
            for d, score in fused[:k]
        ]


class RetrievalAgent:
    """Finds the policy sections that support an answer."""

    name = "Retrieval agent"

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb

    def run(
        self,
        query: str,
        category: str,
        rule_citations: list[str] | None = None,
        policy_question: bool = False,
        max_sections: int = 6,
    ) -> tuple[list[RetrievedSection], list[RetrievedSection]]:
        """Return (sections to cite, raw search ranking).

        ``rule_citations`` are the sections whose rules other agents applied (the
        triage priority rule, the escalation-matrix ``policy_source``, the data
        agent's analysis rule); they are what the answer rests on. Hybrid search
        always runs: its hits are cited when no rule applied or when the user asked
        a policy question, and the ranking is kept in the trace either way.
        """
        search = self.kb.search(f"{category}. {query}", k=3)
        sections: list[RetrievedSection] = []
        for citation in dict.fromkeys(rule_citations or []):
            doc = self.kb.get(citation)
            if doc is not None:
                sections.append(RetrievedSection(citation, doc.metadata["section"], doc.page_content, 0.0))
        if not sections or policy_question:
            have = {s.citation for s in sections}
            sections.extend([h for h in search if h.citation not in have][:2])
        return sections[:max_sections], search
