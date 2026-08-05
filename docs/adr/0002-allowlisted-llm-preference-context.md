# ADR-0002: Allowlisted LLM Preference Context

- **Status:** Accepted
- **Date:** 2026-08-05

## Decision

The optional LLM preference context is a categorical projection, not a profile export. Version 1
allows only these signal names: `topic_overlap`, `category_overlap`, `preference_facet_overlap`,
`explicit_positive_feedback`, `watched_author`, and `watched_institution`. A request may contain at
most four distinct signal names for one candidate.

The projection contains no topic text, profile terms, notes, annotations, collection names, labels,
feedback prose, author names, institution names, or scores. It is sent only to `explain-v1` after
local selection; `judge-v1` receives public paper metadata and quality evidence only. The signal
allowlist is enforced in code and validated in request-capture tests.

## Approval and Rollout

The default configuration remains disabled. Enabling it requires a documented field-level review,
passing the request/artifact privacy tests, and an explicit manual production canary input. The
workflow never enables the context on scheduled runs. The manifest records whether the context was
enabled, and its cache namespace changes when the mode changes.

## Consequences

The model can produce a more specific relevance explanation without receiving the protected profile.
Adding a new signal requires changing this contract, its tests, and the trust-boundary review; a
configuration toggle alone cannot expand the data allowlist.
