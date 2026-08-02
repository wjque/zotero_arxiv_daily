# Changelog

## v0.1.2 - 2026-08-02

### Changed

- Replaced the opaque public Zotero library revision with a timestamped profile snapshot and made
  batch-status metadata visually compact.
- Ordered final recommendation cards by local relevance, validated model quality, latest arXiv
  revision time, and canonical arXiv ID without changing diversity selection.

### Compatibility

- Remote-profile v3 and publishable-site v3 carry an optional source-library synchronization
  instant; v1/v2 inputs remain readable and omit unavailable snapshot information.

## v0.1.1 - 2026-08-02

### Added

- Inspectable generation, artifact, profile-library, and successful workflow-run metadata with
  `Asia/Shanghai` presentation and stale-batch indication.
- Bounded watched-author and watched-institution preferences, exact local matching, optional arXiv
  affiliation parsing, and capped inspectable ranking signals.
- Versioned successful-recommendation history with 14-day repeat suppression and 30-day retention.
- An English-only static-site interface, responsive cards, automatic light/dark color support,
  accessible interaction states, and explicit asset-size budgets.

### Changed

- Scheduled generation now targets `18:30 Asia/Shanghai`; delayed scheduled runs skip model calls,
  and peak-time manual runs require an explicit override.
- Production Actions use reviewed immutable Node.js 24-compatible revisions.
- New profile, arXiv-state, recommendation, and publishable output use their v0.1.1 schemas while
  retaining named v0.1.0 compatibility readers.
- Recommendation prose defaults to English, the model prompt/cache namespace is revision-aware,
  and DeepSeek structured output uses bounded deterministic settings and validation retries.

### Fixed

- Paper dates are converted to `Asia/Shanghai` before calendar-date filtering.
- Failed, skipped, or partially deployed runs no longer advance recommendation history.
- Empty incremental arXiv windows retain a bounded pool of recent, unexpired candidates so daily
  recommendations can fall back to historical papers not present in successful-publication history.
- Feedback score adjustments now affect final local selection as well as model-shortlist creation;
  revised paper metadata no longer reuses a stale cached proposal.
- DeepSeek prompts no longer include a copyable illustrative paper ID, preventing otherwise-valid
  structured output from referring to a candidate that was not requested.

### Security

- Watchlists remain in the protected profile and never enter model prompts; browser artifacts
  expose only a bounded match outcome when applicable.
- Artifact inspection now covers generated site assets, private-field patterns, local paths, and
  per-asset size limits.

## v0.1.0 - 2026-08-01

### Added

- Local-first incremental Zotero synchronization and deterministic protected interest profiles.
- Rate-limited, resumable arXiv discovery; bounded DeepSeek recommendations; and validated diversity selection.
- Encrypted static GitHub Pages output, browser-local feedback, and same-repository feedback Issue ingestion.
- Scheduled/manual GitHub Actions publishing with protected state, concurrency control, and safe run diagnostics.

### Security

- Raw Zotero notes, annotations, and PDF content remain local.
- Published recommendation data is encrypted by default and model/provider failures do not expose secrets, prompts, or response bodies.
