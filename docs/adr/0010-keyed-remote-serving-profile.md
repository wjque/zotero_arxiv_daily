# ADR-0010: Keyed Remote Serving Profile

- **Status:** Accepted
- **Date:** 2026-08-20

## Context

Scheduled recommendations must continue while the local Zotero host is offline. The remote runner
therefore needs a durable interest snapshot, but plaintext profile terms, representative papers,
and watched names reveal more of the local library than category-only discovery requires. Keeping
the complete derived profile only on the local host would protect it but would reduce remote ranking
to coarse arXiv categories whenever that host is unavailable.

## Decision

Profile construction produces two separate versioned artifacts. The complete derived local profile
is written with owner-only permissions under the ignored `runtime/` directory. Remote serving
profile schema v5 contains only bounded arXiv categories, controlled domain/method/task facets,
weighted long-term and recent lexical HMAC identities, anonymous paper-level HMAC prototypes, and
HMAC identities for watched authors and institutions. It contains no plaintext free-form topic,
paper identity, watched name, Zotero record, note, annotation, collection, or feedback label.

Lexical and identity values use domain-separated HMAC-SHA256 truncated to 128 bits. A distinct
operator-generated key of at least 32 UTF-8 bytes is supplied as `ZAD_PROFILE_FEATURE_KEY`. The
profile and key are published through standard input to two different GitHub Actions Secrets; the
key is never serialized into the profile. A keyed verifier binds the pair. Publication validates
the pair before changing either Secret, and a runner rejects a missing or mismatched key before a
model call. GitHub cannot update the profile while the local host is off, but the last published
snapshot remains sufficient for scheduled category discovery and local-on-runner ranking.

The remote runner hashes only public candidate metadata, performs all interest matching locally in
the ephemeral job, and sends no serving-profile fields or key to the model provider. Controlled
facets and arXiv categories remain readable inside the protected profile because they are required
for bounded discovery; they are not treated as anonymous. Readers accept schemas v1 through v5,
and the frozen v0.1.2 ranking path uses a separate protected lexical namespace to preserve its exact
term-count behavior.

## Consequences

- A leaked profile alone does not expose plaintext free-form terms or permit an unkeyed dictionary
  match, but anyone who obtains both Secrets can test candidate terms. Repository and environment
  access must therefore remain least-privilege.
- Updating two Secrets cannot be transactional. A workflow that starts between updates detects the
  verifier mismatch, fails closed, and leaves the previous deployed site usable.
- Local profile refresh requires Zotero and the local host. Scheduled remote recommendations do not;
  they use the most recently published snapshot until the next explicit refresh.
- Rotating the feature key requires rebuilding and publishing the profile and key together. Reusing
  a v5 profile with another key is rejected rather than silently producing zero interest matches.
- The v5 ranker keeps long-term interest, recent interest, controlled facets, anonymous prototypes,
  and exact watchlist matches separately inspectable in local scoring components.

## Migration and Rollback

Schema v5 is opt-in through an explicit local `profile build` and `profile publish-github`; existing
v1-v4 Secrets remain readable and do not require the new key. Before first v5 activation, retain an
owner-only copy of the current v4 profile in ignored local storage. A failed v5 run preserves the
previous deployed site. To roll back the profile boundary, republish that retained v4 payload; the
current reader ignores the unused feature-key Secret for legacy schemas. No Zotero, feedback,
candidate, recommendation-history, encrypted-state, or browser schema is migrated.
