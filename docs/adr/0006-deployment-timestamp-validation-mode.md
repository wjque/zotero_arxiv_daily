# ADR-0006: Deployment-Timestamp Validation Mode

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

Rapid production debugging can start another scheduled workflow before a new public batch is due.
Using generation time or prepared state would permit failed or unpublished work to affect suppression,
impressions, or the user-visible site.

## Decision

The encrypted deployment receipt records an aware UTC timestamp only after GitHub Pages succeeds. A
run that starts less than 24 hours after that timestamp enters metadata-only validation. Missing,
prepared, unsuccessful, legacy-untimestamped, future-dated, or at-least-24-hour history selects normal
publication instead.

Validation may update the bounded public arXiv candidate store and append privacy-safe encrypted
validation manifests. It has no model credential or call and cannot accept publishable, history,
impression, pending-publication, site, artifact, upload, or deployment outputs. Successful publication
promotes prepared history; post-deployment state-push reconciliation performs the same promotion and
idempotent impression recording.

## Consequences

- Repeated validation remains inspectable and cannot postpone the next publication indefinitely.
- Failed or prepared batches never suppress candidates.
- A legacy deployment receipt causes one conservative publication before timestamp-based validation.

## Rollback

Remove the validation branch and always select publication. Keep deployment-success history promotion
and reconciliation unchanged.
