# ADR-0001: Encrypt Protected Workflow State

- **Status:** Accepted
- **Date:** 2026-08-05

## Decision

Private workflow state is persisted as one authenticated AES-GCM bundle named
`state.enc.json`. The encryption key is supplied only to GitHub Actions through the separate
`STATE_ENCRYPTION_KEY` secret. It is distinct from the Pages passphrase and is never embedded in
the browser artifact.

The bundle contains validated JSON files for feedback events, recommendation history, model cache,
ranking pointers, deployment receipts, pending reconciliation data, arXiv state, and privacy-safe
run-manifest history. The workflow decrypts into the ephemeral runner, validates required files and
schemas, and writes only the encrypted bundle back to the state branch. A missing state branch is
the only condition that starts an empty state; decryption, transport, validation, or schema errors
fail the run.

## Migration and Rollback

One workflow migration recognizes the legacy plaintext state format, validates its required files,
encrypts it, removes plaintext files from the current branch tip, and pushes the bundle before the
next state transition. A separate manual-only `purge_legacy_state_history` action first validates the
encrypted bundle, then replaces the `state` branch with a single encrypted-root commit using
`--force-with-lease`. Existing plaintext commits are treated as exposed data: they must be removed
from retained public history and the state key must be rotatable before release.

During key rotation, the active key ID is recorded with the envelope and the previous key may be
retained only for one controlled read-and-rewrite migration. A failed migration leaves the previous
usable site and state untouched; it never falls back to an empty state.

## Consequences

Public repository readers can see ciphertext metadata but cannot read feedback, history, caches, or
profile-derived state without the Actions secret. The workflow gains an explicit migration step and
must keep the state secret configured. State manifests remain privacy-safe and are available for
offline efficiency comparisons after decryption on an authorized runner.
