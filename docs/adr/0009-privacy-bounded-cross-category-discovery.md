# ADR-0009: Privacy-Bounded Cross-Category Discovery

- **Status:** Accepted
- **Date:** 2026-08-20

## Context

The released v0.2.1 discovery path queries core arXiv categories and their fixed one-hop adjacent
categories. It can therefore miss a paper whose public category is outside that graph even when the
paper addresses a problem, method, or task represented by the protected interest profile. Sending
profile terms in an arXiv search query would improve lexical recall but would create a new remote
privacy boundary and expose protected interests to a third party.

## Decision

Controlled discovery maps only allowlisted, normalized domain, method, and task facets to a fixed
set of public arXiv categories. Facets must pass a local strength threshold. The planner preserves
the released core and one-hop path, then adds no more than four cross-category bridge queries in a
deterministic order. It sends only category names and a bounded submission-date interval to arXiv.
Facet values never enter the remote query.

Returned bridge papers are accepted locally only when their public title or abstract contains a
planned facet. Candidate identity, URL construction, revision deduplication, ordering, budgets, and
persistence remain locally controlled. One retrieval permits at most 6 core queries, 6 adjacent
queries, 4 bridge queries, 16 total queries, 32 logical requests, 100 results per request, 1,000
fetched candidates, and 200 bridge candidates. The existing arXiv client keeps requests serialized
and applies its bounded retry policy.

Any provider or metadata failure abandons all partially collected results. A recent previous usable
pool is retained without advancing its successful checkpoint; otherwise retrieval fails and the
previous published site remains untouched.

The released `v0.2.1` discovery mode remains the CLI default and the scheduled workflow continues to
use it. `controlled-shadow` requires a distinct local candidate-state path. Production activation is
deferred to V030-M6 and requires explicit operator approval; this decision does not authorize it.

## Consequences

- Synthetic bridge papers can measure coverage that the released category graph cannot provide.
- The category map is intentionally conservative and incomplete. Adding a facet or category requires
  code review, deterministic fixtures, and another privacy-boundary check.
- Exact local facet matching favors precision and inspectability over semantic recall. Missing a
  bridge remains possible and should be measured before changing this boundary.
- The candidate-state schema does not change because it continues to contain public arXiv metadata
  only. Shadow state can be deleted without migrating or modifying production state.

## Rollback

Use the default `v0.2.1` discovery mode and remove the separate ignored shadow-state file. No profile,
candidate-state, recommendation, feedback, or publishable-site migration is required.
