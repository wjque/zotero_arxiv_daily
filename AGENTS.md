# Project Engineering Rules

## 1. Purpose and Scope

This file is the permanent engineering standard for the entire repository. It applies to every agent, contributor, branch, release, refactor, migration, and operational change throughout the lifetime of the project.

This file defines **how work must be performed**. It must not contain temporary stage goals, sprint tasks, current milestone status, or a chronological change history.

- Immutable stage goals and milestone acceptance plans belong in `docs/plans/`.
- Daily development records belong in `docs/logs/`.
- Durable architecture decisions belong in `docs/adr/` when that directory is introduced.
- User-facing setup and usage belong in `README.md` or dedicated operational documentation.
- Release-level summaries belong in `CHANGELOG.md` when releases begin.

If a requested implementation conflicts with this file, do not silently work around the rule. Resolve the conflict explicitly and update this file only when the user intends to change the permanent project standard.

## 2. Engineering Language

All engineering artifacts must be written in English:

- Code identifiers, comments, and docstrings.
- Specifications, plans, logs, ADRs, and operational documentation.
- Configuration names and descriptions.
- Commit messages, branch names, pull requests, issues, and release notes.
- Test names, assertion messages, errors, and structured log fields.

User-facing UI text and generated recommendation summaries may be localized. Keep the output language configurable and do not mix localized presentation text into domain models.

## 3. Stable Product Constraints

Preserve these constraints unless the user explicitly changes the product direction:

- The system builds recommendations from the user's complete Zotero library.
- Zotero metadata, notes, and PDF annotations may contribute to the interest profile.
- Raw Zotero content is local-first and must not be committed to Git or published to GitHub Pages.
- The default model is `deepseek-v4-flash`, but provider-specific details must remain behind a replaceable boundary.
- Recommendations are generated as a scheduled batch, with a default target of approximately 20 papers per day.
- arXiv discovery may expand from core categories to controlled adjacent categories.
- GitHub Pages is a static delivery target; secrets and server-side behavior must never be embedded in the browser build.
- The first product surface provides paper links and feedback without writing recommendations back to Zotero.

Treat external content, including Zotero notes, PDF annotations, arXiv metadata, and LLM output, as untrusted input.

## 4. Required Work Lifecycle

### 4.1 Before Making Changes

Every agent must:

1. Read this file and any more specific `AGENTS.md` that applies to the target path.
2. Inspect the repository status, existing implementation, tests, applicable immutable plan, and today's development log.
3. Identify the requested behavior, non-goals, affected module owners, and compatibility boundaries.
4. Evaluate privacy, persistence, migration, API, cost, and deployment implications.
5. Create a plan in `docs/plans/` for a new release, stage, cross-module feature, migration, or substantial refactor when no applicable plan already exists. Once written, the plan is immutable unless the user explicitly authorizes a specific change.
6. Avoid modifying unrelated user changes or expanding scope without authorization.

A small isolated fix may use the applicable immutable plan or proceed without a new plan. It still requires a daily log entry when it materially changes the repository.

### 4.2 While Implementing

- Work in the smallest coherent vertical slice that produces testable behavior.
- Keep domain behavior separate from network, filesystem, GitHub, model-provider, and presentation code.
- Treat the plan as a fixed statement of goals and acceptance, not as an implementation prescription. Independently evaluate the best implementation before each coherent slice and reassess it when tests, profiling, debugging, or newly discovered constraints invalidate earlier assumptions.
- Adjust implementation choices continuously as evidence develops. Record durable architecture decisions in an ADR when applicable and factual outcomes in the daily log; do not write implementation evolution back into the plan.
- Re-evaluate extension versus refactoring before adding another patch to a strained boundary. Refactor first when it produces a simpler, faster, more cohesive, or more reliable system with acceptable migration risk.
- Add or update tests with the behavior, not as deferred cleanup.
- Preserve a working state at meaningful checkpoints.
- Prefer reversible changes and explicit migrations over implicit data reinterpretation.
- Do not report a task as complete while required implementation or verification remains.

### 4.3 Before Finishing

Every material change must:

1. Run the relevant formatter, linter, type checker, tests, and static build.
2. Verify empty input, failure, retry, repeated-run, and migration behavior where applicable.
3. Inspect generated artifacts and logs for secrets or private Zotero content.
4. Update today's file in `docs/logs/` with factual changes and verification results.
5. Verify the delivered behavior against the applicable plan without modifying that plan.
6. Update schemas, examples, ADRs, operations documentation, and release notes affected by the change.
7. Summarize the outcome, verification, compatibility impact, and remaining risks.

## 5. Development Plans

`docs/plans/` contains immutable stage goals and milestone-level product contracts. Plans define what
must be delivered and how it will be accepted. They do not prescribe how agents must implement it.

### 5.1 Plan Immutability

- A plan becomes immutable as soon as it is written to the repository.
- Agents must not edit an existing plan to change status, dates, scope, milestones, acceptance
  criteria, decisions, progress, outcomes, or wording unless the user explicitly authorizes that
  specific plan change.
- Existing plans remain unchanged and are not retrofitted to this standard.
- Implementation discoveries do not authorize a plan edit. Keep working toward the fixed outcome by
  selecting and adjusting the best implementation, and record factual results in the daily log.
- If a discovered constraint makes a goal or acceptance criterion impossible or unsafe, stop and ask
  the user whether to change the plan. Do not silently weaken acceptance.
- Create another plan only for a genuinely distinct future stage or scope. Do not create a rewritten
  copy merely to bypass plan immutability.
- Plan completion and release progress are evidenced by tests, logs, changelogs, and release records;
  agents do not maintain mutable status fields or checklists inside a frozen plan.

### 5.2 Plan File Naming

Use one of these formats:

- Release or version plan: `vMAJOR.MINOR.PATCH-kebab-case-title.md`
- Non-release investigation or migration plan: `YYYY-MM-DD-kebab-case-title.md`

Examples:

```text
docs/plans/v0.1.0-engineering-foundation.md
docs/plans/v0.2.0-local-interest-profile.md
docs/plans/2026-08-03-profile-schema-migration.md
```

Do not create `final`, `new`, `revised`, `v2`, or copy-suffixed plan files. A replacement for a
materially different future scope must use its own stage, version, or date identity and may reference
the earlier immutable plan without editing it.

### 5.3 Required Plan Content

Every new plan is intentionally concise and contains only:

- Title and target version, when applicable.
- The stage objective, scope, and explicit non-goals.
- Milestones with stable identifiers and the user-visible or system behavior each milestone must
  deliver.
- A measurable acceptance method for the stage and for every milestone, including required quality,
  compatibility, privacy, migration, performance, or recovery outcomes when relevant.

Plans must not contain implementation architecture, module/file assignments, algorithms, proposed
classes or functions, step-by-step implementation sequences, design alternatives, progress status,
decision/change notes, daily activity, debugging history, or final outcome reports. Agents own the
implementation strategy and may change it throughout development without changing the plan.

## 6. Daily Development Logs

All material changes must be recorded under `docs/logs/` using the project timezone, `Asia/Shanghai`.

### 6.1 Log File Naming

Use one file per calendar day:

```text
docs/logs/YYYY/YYYY-MM-DD.md
```

Example:

```text
docs/logs/2026/2026-07-31.md
```

Agents working on the same day append to the existing file. They must not overwrite, reorder, or duplicate other entries.

### 6.2 Required Log Content

A daily log starts with the date and contains concise factual sections as needed:

```markdown
# YYYY-MM-DD

## Summary

## Added

## Changed

## Fixed

## Refactored

## Removed

## Tests and Verification

## Compatibility and Migration

## Follow-ups
```

Omit empty sections. Each material entry should identify:

- The affected feature or module.
- What behavior was added, changed, fixed, refactored, or removed.
- The target version, branch, or plan when known.
- User-visible, schema, configuration, migration, privacy, or deployment effects.
- Commands or checks used for verification and their result.
- Known limitations or follow-up work that remains.

Logs record outcomes, not raw thought processes, shell-command transcripts, or trivial formatting churn. Documentation-only governance changes are material and must also be logged.

Daily logs do not replace release notes. At release time, summarize relevant daily entries into `CHANGELOG.md` and the GitHub Release.

## 7. Versioning and Branch Strategy

### 7.1 Versioning

Use Semantic Versioning: `MAJOR.MINOR.PATCH`.

- `MAJOR`: incompatible public CLI, configuration, API, persisted-data, or deployment changes after stability guarantees apply.
- `MINOR`: backward-compatible functionality or a planned capability increment.
- `PATCH`: backward-compatible fixes, security patches, and small internal improvements.

Pre-1.0 versions may evolve quickly, but configuration, stored data, feedback, profiles, and generated recommendation schemas must still be explicitly versioned and migrated.

Maintain one authoritative application version source. Do not duplicate hard-coded version strings across modules.

Use immutable Git tags in the form `vMAJOR.MINOR.PATCH`. Never move or reuse a published tag.

### 7.2 Branches

Use a GitHub-flow-based strategy with short-lived branches:

- `main`: always releasable; protected after GitHub setup.
- `feature/<issue-or-plan>-<kebab-description>`: new behavior.
- `fix/<issue-or-plan>-<kebab-description>`: non-emergency fixes.
- `refactor/<issue-or-plan>-<kebab-description>`: behavior-preserving structural work.
- `docs/<issue-or-plan>-<kebab-description>`: documentation-only changes.
- `release/vMAJOR.MINOR`: stabilization for an upcoming minor or major release.
- `hotfix/vMAJOR.MINOR.PATCH-<kebab-description>`: urgent production correction from the released tag.
- `support/vMAJOR.MINOR`: optional maintenance branch only when parallel support is explicitly required.

Examples:

```text
feature/v0.2-profile-incremental-sync
fix/142-arxiv-id-normalization
refactor/v0.3-ranking-boundaries
release/v0.3
hotfix/v0.3.1-secret-redaction
```

Do not create a permanent `develop` branch without an approved need. Delete merged short-lived branches. Do not keep long-running version branches merely to avoid integrating changes.

### 7.3 Release Flow

1. Use the applicable immutable version plan in `docs/plans/` as the release contract.
2. Develop features and fixes in short-lived branches from `main`.
3. Create `release/vMAJOR.MINOR` only when coordinated stabilization is necessary.
4. Freeze persisted schemas and configuration before final release verification.
5. Update `CHANGELOG.md`, migration guidance, and version metadata.
6. Run the complete quality and security gates.
7. Merge to `main`, create the immutable version tag, and publish the GitHub Release.
8. Merge release-only fixes back into active development and remove the release branch when no longer needed.

### 7.4 Versioned File Naming

Version history belongs in Git, not duplicated filenames.

Never create source files such as:

```text
ranking_v2.py
profile_new.py
client_final.py
config_old.yaml
```

Keep stable descriptive filenames and evolve their implementation through commits and migrations.

Allowed versioned filenames are limited to artifacts where ordering or coexistence is part of the design:

- Migrations: `NNNN_kebab_or_snake_description.py`.
- Version plans: `vMAJOR.MINOR.PATCH-kebab-title.md`.
- Dated logs and immutable generated snapshots: ISO `YYYY-MM-DD` dates.
- Explicit protocol or schema fixtures when multiple versions must be tested together.

Compatibility implementations must use named adapters or schema namespaces, not vague `old`, `new`, or `final` suffixes.

## 8. Repository and Module Structure

Organize production code by stable feature responsibility. The expected top-level shape is:

```text
src/zotero_arxiv_daily/
  core/          # Small stable primitives: configuration, errors, time, shared types
  zotero/        # Zotero access, normalization, and incremental synchronization
  profile/       # Item digests and interest profile construction
  arxiv/         # Querying, throttling, parsing, and normalization
  ranking/       # Pre-ranking, quotas, diversity, and deduplication
  llm/           # Provider contract, prompts, and provider adapters
  feedback/      # Feedback models, import, aggregation, and consumption
  pipeline/      # Use-case orchestration and checkpoints only
  site/          # Publishable models and static site generation
  security/      # Redaction, minimization, encryption, and output validation
  storage/       # Explicit persistence adapters and migrations
```

Additional rules:

- Create directories only when they contain real responsibilities; do not scaffold empty architecture.
- Keep related models, behavior, and adapters close to the feature that owns them.
- Split a file when it contains unrelated responsibilities, changes for unrelated reasons, or cannot be tested independently. Do not split files merely to satisfy a line-count target.
- Do not create one file per trivial class or function.
- Do not use generic dumping grounds such as `utils.py`, `helpers.py`, `misc.py`, or an unconstrained `common/` package.
- `core/` contains only genuinely cross-cutting, stable primitives. Feature-specific behavior must remain in its feature module.
- `pipeline/` coordinates use cases; it must not implement provider parsing, ranking formulas, or UI rendering.
- `scripts/` may wrap maintenance commands but must not own business logic.
- Tests mirror production ownership under `tests/unit`, `tests/contract`, `tests/integration`, and `tests/e2e`.
- Generated data, runtime state, caches, secrets, and real Zotero exports do not belong in the source tree.
- Package `__init__` files contain no hidden execution or mutable global initialization.

Dependencies should point toward stable domain behavior. For example, ranking logic must not depend on the arXiv HTTP client, profile construction must not depend on GitHub, and browser code must not depend on model credentials.

## 9. Code Quality and Design

### 9.1 Simplicity and Reuse

- Prefer explicit, cohesive code over clever compression.
- Optimize for the simplest coherent end state, not the smallest diff. Do not preserve awkward
  ownership, duplication, or obsolete paths merely because patching them requires fewer edits.
- Extract reuse when repeated behavior has the same semantics and ownership, not merely similar syntax.
- Keep functions focused, inputs explicit, and side effects visible.
- Use typed models at module and external boundaries; do not pass unvalidated dictionaries through the system.
- Prefer immutable domain values and explicit state transitions.
- Avoid mutable global state and implicit singletons.
- Inject clocks, randomness, HTTP clients, storage, and providers when deterministic behavior or testing requires control.
- Add interfaces only for real external boundaries, multiple implementations, or an approved replacement need.
- Remove dead code, obsolete flags, unused compatibility paths, and dependencies promptly.

### 9.2 Efficiency

- Measure before optimizing and record the relevant baseline.
- Prefer incremental processing, bounded batches, connection reuse, and cached stable results.
- Detect and prevent N+1 network or model calls.
- Bound candidate counts, retries, prompt size, token usage, memory, and concurrency.
- Use synchronous code by default when concurrency provides no measured benefit. arXiv access must respect its documented serialized rate limit.
- Do not introduce concurrency, a queue, an ORM, a vector database, or another service solely because it may be useful later.
- Performance changes must preserve correctness, determinism where required, and observability.

### 9.3 Boundaries and External Services

- Parse and validate external responses immediately at the adapter boundary.
- Convert provider exceptions into contextual application errors without losing the root cause.
- Set explicit connect, read, and total timeouts.
- Retry only transient failures with bounded backoff and jitter.
- Reuse network clients and close them deterministically.
- Keep provider-specific fields out of domain models unless they are part of a deliberate provider-neutral contract.
- LLM output may propose ranks, summaries, and explanations, but may never directly control URLs, persistence, checkpoints, secrets, security policy, or executable actions.

### 9.4 Error Handling and Observability

- Fail fast on invalid configuration and violated invariants.
- Do not swallow broad exceptions.
- Use structured logs with stable fields such as `run_id`, stage, duration, count, retry, model, and schema version.
- Do not log secrets, raw notes, raw annotations, PDF text, full prompts, or complete protected profiles.
- Every scheduled run must produce an inspectable result manifest without private content.
- Preserve the previous usable output when a new batch fails.

## 10. Refactoring Standard

Every non-trivial change requires an extension-versus-refactor assessment before implementation and
another assessment when debugging reveals misplaced responsibilities, duplicated rules, excess
state, or an inefficient data flow. The agent owns this assessment and must revise the implementation
approach as evidence develops.

Consider refactoring first when one or more are true:

- The change crosses several modules because responsibilities are misplaced.
- Extending the current design would duplicate rules, state, provider calls, or compatibility branches.
- The existing boundary prevents independent testing or reuse.
- The change would require repeated conditionals for old and new behavior.
- Measured performance problems are caused by the current ownership or data flow.
- A simpler model can remove more code and states than the new feature adds.

Refactor when it can materially improve runtime efficiency, reduce total code or state, clarify
ownership, remove duplication, improve testability, or prevent patch-on-patch failure modes at an
acceptable migration risk. In those cases, do not layer a local workaround onto the existing design.
Performance claims require measurement; structural simplification may be justified by concrete code
and state reduction. Do not refactor solely for aesthetic preference or introduce speculative
abstractions.

For a substantial refactor:

1. Preserve the immutable plan. Add an ADR when the refactor changes a durable architecture,
   ownership, persistence, or trust-boundary decision.
2. Add characterization tests for behavior that must remain stable.
3. Establish a benchmark when efficiency is part of the justification.
4. Move responsibilities incrementally through explicit boundaries.
5. Implement the requested behavior on the improved structure.
6. Remove the superseded path and temporary compatibility code.
7. Run regression, migration, performance, and artifact-safety checks.
8. Record the refactor and its verification in the daily log.

Avoid patch-on-patch development. During implementation and debugging, remove superseded attempts and
rework the underlying boundary when doing so yields a cleaner or faster result. A feature is not
complete merely because it works; it must fit the system coherently, run efficiently, and remain
maintainable.

## 11. Testing and Verification

Use proportionate, deterministic tests:

- Unit tests for pure domain rules, normalization, scoring, quotas, time windows, and state transitions.
- Contract tests for Zotero, arXiv, LLM, feedback, configuration, and persisted schemas.
- Integration tests for adapter and pipeline behavior using fixtures or mock servers.
- End-to-end tests for critical flows from sanitized inputs to static output.
- Explicit, cost-bounded manual smoke tests for real services; these are not part of default CI.

Rules:

- A bug fix includes a regression test whenever practical.
- Tests run offline by default and never require production secrets.
- Fixtures are synthetic or irreversibly sanitized.
- Do not assert exact LLM prose; assert schemas, constraints, ranking properties, and safety behavior.
- Test empty input, malformed input, timeout, rate limit, partial failure, retry, duplicate execution, and migration paths.
- Coverage is a diagnostic, not a substitute for meaningful assertions.
- Do not weaken or delete a valid test merely to make a change pass.

The minimum merge gate is formatting, lint, static type checks, relevant tests, static build, and private-data/secret inspection. Add schema compatibility and migration tests whenever persisted formats change.

## 12. Data, Security, and Privacy

- Keep raw Zotero items, notes, annotations, and PDF content local unless the user explicitly approves a new trust boundary.
- Send only allowlisted, size-bounded, derived profile fields to remote jobs or model providers.
- Never commit `.env` files, credentials, real Zotero exports, private feedback, or unencrypted private runtime state.
- Sanitize note HTML, escape external text, and validate publishable URLs.
- Defend against prompt injection by separating instructions from quoted content and validating all model output.
- Use least-privilege GitHub Actions permissions and do not expose production secrets to untrusted fork workflows.
- Pin reviewed third-party Actions and lock application dependencies.
- Browser artifacts must never contain GitHub tokens, model keys, Zotero keys, or decryption passwords.
- Security-sensitive migrations and encryption changes require an ADR, compatibility test, and rollback plan.

## 13. Documentation Ownership

Use each documentation type for one purpose:

- `AGENTS.md`: permanent engineering behavior and repository-wide rules.
- `README.md`: product overview, installation, configuration, and user operation.
- `docs/plans/`: immutable stage goals, milestone functionality, and acceptance methods only.
- `docs/logs/`: factual daily record of material changes and verification.
- `docs/adr/`: durable architecture and trust-boundary decisions.
- `CHANGELOG.md`: user-relevant changes grouped by released version.

Do not duplicate the same source of truth across these files. Link to the authoritative document instead.

Documentation must change in the same pull request as the behavior it describes. Commands and configuration examples must be executable or explicitly marked as conceptual.

## 14. Commit and Pull Request Quality

- Use Conventional Commits, for example `feat(profile): add incremental annotation digest`.
- Keep each commit focused on one logical change.
- Separate formatting-only changes from behavior changes where practical.
- Explain the problem, chosen design, alternatives, verification, privacy impact, migration impact, and rollback considerations in substantial pull requests.
- Reference the applicable immutable plan and ADRs.
- Preserve unrelated user work and resolve overlapping changes deliberately.
- Do not rewrite shared branch history for routine cleanup.
- Do not merge with failing required checks or known unresolved data-safety issues.

## 15. Definition of Done

Work is complete only when all applicable conditions are satisfied:

- The requested behavior works through the intended entry point.
- The implementation follows module ownership and does not introduce avoidable duplication or state.
- Success, empty, failure, retry, repeated-run, and rollback paths are safe.
- Tests, formatting, lint, types, builds, and artifact inspections pass.
- Persisted formats and configuration remain compatible or include a tested migration.
- Security and privacy boundaries remain intact.
- The delivered behavior satisfies the applicable immutable plan's acceptance methods; progress and
  design changes are recorded outside the plan.
- Today's development log records the material outcome and verification.
- Relevant README, ADR, schema, configuration, migration, and release documentation is current.
- Temporary compatibility code is removed or has an explicit owner and removal milestone.
- Remaining limitations and risks are reported clearly.
