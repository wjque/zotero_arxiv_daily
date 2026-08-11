# ADR-0007: Evidence-Bounded Scientific Value Gates

- **Status:** Accepted
- **Date:** 2026-08-11

## Context

An average scientific-quality score can allow a clearly incremental paper to pass when contribution
clarity or presentation quality offsets weak solution value. Limitation prose also becomes
uninformative when it merely reports that a paper has no extracted limitations section. The product
needs critical assessment without converting unavailable evidence into a negative scientific claim
or granting the model direct selection authority.

## Decision

The versioned `judge-v5` contract separately assesses solution advance and technical depth from
bounded public candidate evidence. Routine recombination, component substitution, parameter tuning,
or a direct architecture extension without demonstrated meaningful gain receives a weak solution
advance assessment. Technical depth measures substantive mechanism, reasoning, and validation rather
than component count; a simple method may score strongly when evidence supports a non-obvious insight
or large robust gain.

Local deterministic selection rejects a candidate when either assessment is below `0.50` and model
confidence is at least `0.50`. Unknown values and lower-confidence assessments do not trigger the
gate. Scientific relevance still comes from the protected local profile and fixed ranking weights;
the model cannot select identifiers, change thresholds, or control persistence.

The versioned `explain-v3` contract receives the selected paper's bounded method and evaluation
evidence. It must derive a paper-specific critical limitation from any supplied evidence instead of
falling back solely because no limitations section was extracted. Inferred risks remain explicitly
distinct from author-stated limitations.

Quality-reference policy v2 treats approved methodology and evidence criteria as evaluation
references. A demonstrated failure may affect the relevant dimension only when supplied candidate
evidence is sufficient to assess the criterion. Missing or unavailable evidence remains unknown.

## Consequences

- Judge and explanation caches use new contract namespaces.
- Existing quality-reference policy v1 profiles retain `judge-v4` behavior and require explicit
  generation and approval of a policy-v2 profile before the new assessment becomes active.
- A batch may contain fewer than the target count when sufficiently supported assessments identify
  weak solution advance or technical depth.
- Raw Zotero content, profile terms, source paper identities, and feedback prose remain outside model
  payloads.

## Rollback

Approve the preceding policy-v1 quality profile to restore `judge-v4`, or use the existing v0.1.2
ranking rollback workflow. Both paths retain encrypted state and current publishable-schema readers.
