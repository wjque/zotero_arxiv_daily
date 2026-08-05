# ADR-0003: Exact Evaluation Candidate Identity

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

Curated Zotero labels can identify an arXiv paper through `arxiv:<id>` or the canonical public
DOI `doi:10.48550/arxiv.<id>`. Production retrieval intentionally retains only a bounded recent
public candidate pool, so older labeled papers are not expected to appear in that pool. Treating a
zero intersection as a ranking result made Recall@60 appear as `0.0` even when the ranker had no
opportunity to score a labeled paper.

## Decision

Evaluation normalizes only exact identity aliases. ArXiv DOI aliases map to the canonical arXiv ID;
titles, authors, and lexical similarity never create identity matches. A frozen evaluation snapshot
may be hydrated with exact public arXiv metadata through `evaluate hydrate-candidates`. The hydrated
state is evaluation-only, may retain older records, and is never read by production retrieval or
recommendation runs. Reports expose candidate overlap and candidate-conditional Recall separately
from raw corpus Recall.

## Consequences

- Zero overlap remains an explicit data-coverage warning instead of a ranking-quality score.
- Held-out labels with resolvable arXiv identities can produce interpretable conditional metrics.
- Non-arXiv DOI labels remain unresolved until an explicit public identity mapping is curated.
- The evaluation candidate state must not be used as a production candidate-state path.

## Rollback

Delete the evaluation-only hydrated state and use the normal bounded retrieval state. No production
data or persisted recommendation schema depends on the hydration command.
