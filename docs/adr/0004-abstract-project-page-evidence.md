# ADR-0004: Abstract Project Page Evidence

- **Status:** Accepted
- **Date:** 2026-08-06

## Context

v0.2.0 needs a preliminary open-source signal without searching for repositories or trusting model
claims. Paper abstracts are untrusted and may contain arbitrary links, so following every supplied URL
would create an unnecessary server-side request-forgery and availability boundary.

## Decision

Fine ranking may inspect only HTTPS project URLs explicitly present in the public arXiv abstract and
hosted on an allowlisted code or project-page domain. Requests have fixed timeouts, bounded URL and
redirect counts, no implicit redirects, no credentials, no non-standard ports, no response-body
retention, and a daily candidate cache. Every redirect is revalidated against the same allowlist.

A successful 2xx response supplies a positive project-page availability feature. Missing links,
timeouts, transient failures, rejected hosts, and failed validation supply no feature and never become
negative evidence. Reachability is only an open-source proxy; it does not prove that source code,
licensing, reproducibility, correctness, or maintenance is available.

## Consequences

- The model judges preliminary scientific quality from public titles and complete abstracts only.
- The model cannot invent, select, or validate the project URL used by ranking.
- A project-page outage cannot fail the recommendation batch or penalize a paper.
- Adding another host or changing request bounds requires a trust-boundary review and tests.

## Rollback

Disable the project-page client at the refined-pipeline boundary. Fine ranking then treats the feature
as unavailable and continues with interest, recency, watched identity, and abstract quality signals.
