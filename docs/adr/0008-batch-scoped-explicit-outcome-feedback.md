# ADR-0008: Batch-Scoped Explicit Outcome Feedback

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

The original browser protocol stored one action per paper. It could express pre-reading interest or
reading completion, but it could not distinguish either signal from the user's explicit judgment
that a paper was worthwhile after reading. It also lacked a publication-batch identity, making a
repeated paper ambiguous. The v0.3 objective requires exact worthwhile-read counts without treating
silence, an unread paper, or a delayed judgment as negative evidence.

## Decision

Browser feedback protocol v2 stores at most one action for each of three independent stages:
pre-reading preference, reading completion, and post-reading value. The two post-reading outcomes are
`worthwhile` and `not_worthwhile`; either requires an explicit `read` action for the same paper and
publication batch. A later explicit action for the same stage is an append-only correction that
supersedes the prior active event. Repeated identical actions are idempotent.

New feedback records carry the publication batch ID and are attributed only to a matching successful
impression. No fallback may move a batch-scoped outcome to another exposure of the same paper. The
legacy Issue protocol remains readable. Pending browser-local v1 actions migrate as
`legacy-unattributed`, preserving the explicit action without guessing a batch.

The local report contains only allowlisted per-batch counts and ratios: impressions, papers with any
explicit feedback, reading completions, explicit post-reading outcomes, worthwhile and
not-worthwhile reads, post-reading outcome coverage among completed reads, the worthwhile rate among
explicitly labeled reads, and explicit-feedback coverage. Missing feedback remains unknown and never
contributes to a negative count or rate denominator.

## Consequences

- Reading completion and post-reading value can be measured separately for every recorded batch.
- Delayed judgments and corrections remain attributable without rewriting ledger history.
- A browser can retain submitted state and export only newly changed stages.
- Legacy browser actions remain usable but do not contribute to a guessed batch total.
- The external payload and browser state gain a new schema version; persisted feedback ledger schema
  v2 remains unchanged because it already supports batch IDs, corrections, and the new outcomes.
- Raw feedback events remain encrypted protected state and are not added to model payloads or public
  site artifacts.

## Rollback

The ingestion boundary continues to accept feedback Issue schema v1, and the ledger retains its
existing schema-v1 migration. Rolling back the site to v0.2.1 stops collecting post-reading outcomes
but does not invalidate already persisted events. Outcome reports are read-only and can be omitted
without changing recommendation or publication behavior.
