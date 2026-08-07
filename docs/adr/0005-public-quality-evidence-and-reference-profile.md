# ADR-0005: Public Quality Evidence and Approved Reference Profile

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

Scientific-quality assessment needs more evidence than an abstract, but public papers and linked
repositories are untrusted. Quality preferences may also reflect explicit reading feedback, while raw
Zotero content, source identities, and feedback prose must remain inside the protected boundary.

## Decision

The fine-screening boundary may fetch only a bounded ar5iv HTML URL derived from a canonical arXiv ID.
It extracts allowlisted method, implementation/evaluation, and limitations sections under per-section
and total character limits. It may inspect only root-structure metadata for an explicitly linked,
reachable GitHub repository. Repository code is never downloaded, cloned, imported, or executed.

Quality-reference profiles contain fixed allowlisted trait names and normalized aggregate support.
Profiles are built only from explicitly approved structured examples and explicit feedback events.
Generated versions are immutable; a separate protected pointer controls approval and rollback. Model
payloads receive only the approved aggregate profile and never source paper IDs or feedback records.

Model assessments remain untrusted proposals bound to requested arXiv IDs and supplied evidence field
names. Local code owns URLs, evidence acquisition, scoring weights, persistence, and publication.

## Consequences

- Missing, malformed, oversized, timed-out, or unavailable evidence remains unknown.
- Public section text is explicitly described as quoted untrusted data in the system contract.
- Published schema v5 reports quality, uncertainty, implementation evidence, and provenance separately.
- Quality-profile versions and aggregate counts are inspectable without exposing protected sources.

## Rollback

Disable the paper-section, repository-material, or quality-profile clients at the refined-pipeline
boundary. Existing v1-v4 publishable readers and the v0.1.2 ranking mode remain available.
