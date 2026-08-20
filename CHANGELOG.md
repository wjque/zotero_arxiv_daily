# Changelog

## v0.3.0 - Unreleased

### Added

- Added batch-scoped browser feedback for separate pre-reading preference, reading completion, and
  explicit worthwhile or not-worthwhile post-reading outcomes, including delayed submissions,
  append-only corrections, and idempotent repeated submissions.
- Added a privacy-safe local outcome report with per-batch worthwhile-read counts, reading
  completions, post-reading coverage, worthwhile rates among explicitly labeled reads, and explicit
  feedback coverage without interpreting missing outcomes as negative.
- Added deterministic, bounded cross-category discovery for local shadow evaluation, including
  synthetic bridge-paper coverage, local facet acceptance, hard query and candidate budgets, and
  atomic fallback to the previous usable pool.

### Changed

- Retain submitted browser feedback state while exporting only newly changed stages, and isolate
  feedback for a repeated paper by publication batch.
- Migrate pending browser schema-v1 actions without guessing a historical batch.
- Keep the released v0.2.1 category path as the production default while requiring controlled
  cross-category runs to use a separate candidate-state file.

### Compatibility and Security

- Existing feedback Issue schema v1 and persisted feedback-ledger schemas remain readable. New
  external identifiers and action sequences are bounded and validated before atomic ingestion.
- Missing feedback remains unknown. Raw feedback and paper identities remain in encrypted protected
  state; aggregate reports expose only allowlisted counts and ratios.
- Cross-category arXiv requests contain only public category names and date intervals. Protected
  profile facets remain local, the public-candidate state schema is unchanged, and controlled shadow
  state can be removed without migration.

## v0.2.1 - 2026-08-18

### Added

- Added bounded public method, implementation/evaluation, and limitation-section extraction plus
  graded explicitly linked GitHub implementation-material evidence without cloning or executing code.
- Added immutable quality-reference profiles built from operator-approved structured examples and
  explicit feedback, with encrypted protected-state persistence and reversible approval pointers.
- Added a protected manual quality-profile maintenance workflow with privacy-safe inspection,
  separate generation and activation, encrypted fast-forward persistence, and first-use deactivation.
- Added sub-24-hour metadata-only validation runs with privacy-safe encrypted manifests and no model,
  site build, Pages deployment, publication history, impression, or pending-state changes.
- Added publishable schema v5 fields that report quality evidence, uncertainty, implementation
  evidence, and bounded provenance separately while retaining v1-v4 readers.

### Changed

- Upgraded default quality assessment to policy v2, `judge-v5`, and `explain-v3`. Solution advance
  and technical depth now have confidence-bounded local selection gates, while final explanations use
  available method and evaluation evidence for paper-specific critical assessment instead of relying
  on an extracted limitations section. Existing policy-v1 profiles retain `judge-v4` until an
  operator explicitly generates and approves a policy-v2 profile.
- Treat methodology and evidence standards as evaluation references under policy v2. Demonstrated
  failures may affect relevant dimensions only when supplied candidate evidence is sufficient;
  missing or unavailable evidence remains unknown, and profile support remains non-scoring.
- Activated the quality-first production weights: interest `0.40`, recency `0.05`, watched identity
  `0.10`, scientific quality `0.35`, and project/implementation evidence `0.10`.
- Moved bounded project-page reachability into coarse screening and expanded fine screening from
  title/abstract-only evidence to allowlisted public paper sections.
- Made approved quality-reference use visible only through aggregate protected manifest fields;
  source paper identities and feedback content never enter model payloads or public artifacts.

### Fixed

- Promoted recommendation history only after a successful Pages deployment, including deterministic
  next-run reconciliation after a post-deployment protected-state push failure.

### Compatibility and Security

- Existing publishable schemas v1-v4, protected state, and the explicit v0.1.2 ranking rollback remain
  supported. New quality and validation files are accepted only inside the AES-GCM state bundle.
- Unavailable public evidence remains unknown rather than a negative scientific claim. Public content
  is length bounded, treated as untrusted quoted data, and cannot introduce identifiers or URLs.

## v0.2.0 - 2026-08-06

### Added

- Added a versioned normalized ranker with applicability-aware interest, recency, and watched-identity
  coarse features, reversible weight pointers, diversity constraints, and bounded exploration.
- Added local curated-corpus snapshots, baseline/shadow evaluation, calibration, candidate-label
  overlap reporting, and feature-group ablations without exposing private labels.
- Added append-only feedback v2 with impression-aware outcome attribution and exact v1 compatibility;
  v0.2.0 stores this evidence without applying it to ranking.
- Added provider-neutral `judge-v3` and `explain-v2` refinement with complete-record adaptive
  batching, evidence-bound quality anchors, uncertainty, separate caches, bounded retries, and
  final-candidate-only prose generation.
- Added bounded reachability evidence for approved project-page links explicitly supplied in public
  abstracts as a positive-only open-source proxy.
- Added publishable schema v4 quality, uncertainty, preference-signal, and limitation fields with
  exact v1-v3 readers and encrypted desktop/mobile browser execution tests.
- Added provider-reported token, latency, and cost fields to private run manifests, bounded
  manifest-history retention, and an aggregate-only efficiency observation path. The quality-first
  canary does not require an efficiency comparison.

### Changed

- Expanded protected profile schema v4 with weighted long-term/recent interests and bounded
  domain, method, and task facets derived locally from the complete Zotero library.
- Removed URL/domain fragments and reserved feedback-label tags from local interest extraction, with
  an extractor-version bump that invalidates stale digests before profile export.
- Hardened arXiv retries, response limits, candidate-pool degradation, workflow state restoration,
  and post-deployment state reconciliation while preserving the previous usable site on failure.
- Added optional exact-identity public evidence adapters and kept missing or inapplicable evidence
  distinct from negative quality evidence.
- Split raw corpus Recall from candidate-pool coverage and candidate-conditional Recall so a
  zero-overlap corpus cannot be misreported as a coarse-ranking regression.
- Made all provisional ranking and efficiency metrics observation-only for v0.2.0; release acceptance
  depends on the production canary's shortlist quality, privacy, reliability, and recovery checks.

### Compatibility

- Existing remote-profile v1-v3, feedback v1, arXiv state v1-v3, recommendation history, and
  publishable site v1-v3 remain readable through explicit adapters or migrations.
- The frozen v0.1.2 ranker is available through an explicit manual workflow rollback mode while the
  current encrypted state, schema readers, and deployment protocol remain active. Scheduled runs
  continue to use the v0.2 path.

### Security

- Raw Zotero content, curated labels, free-text feedback, prompts, and credentials remain excluded
  from browser artifacts and public state. New model preference fields remain disabled until an
  explicit field-level trust-boundary approval.
- Protected workflow state now uses a separate AES-GCM key and one encrypted bundle. A manual,
  lease-protected state-history purge is available after verified migration to expunge legacy
  plaintext commits.

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
