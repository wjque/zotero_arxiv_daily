# Zotero arXiv Daily

Zotero arXiv Daily is a local-first tool that builds a compact interest profile from a
Zotero library and uses it to produce a daily arXiv reading list. Raw Zotero records,
notes, annotations, and PDF content remain local.

The v0.2.0 release adds interest-based coarse screening, bounded abstract-quality and
project-page refinement, append-only reading feedback collection, and a backward-compatible encrypted
site schema. Its immutable acceptance contract is the
[v0.2.0 plan](docs/plans/v0.2.0-personalized-ranking-quality.md).

## Requirements

- Python 3.12 or newer
- `uv` for reproducible development environments
- Zotero Desktop with its Local API enabled, when checking the local connection

## Development setup

```bash
uv sync --all-groups
uv run zotero-arxiv-daily doctor
```

To use a non-default configuration file, pass a TOML or JSON file explicitly:

```bash
uv run zotero-arxiv-daily --config ./zotero-arxiv-daily.toml doctor
```

Configuration values are resolved in this order: built-in defaults, configuration file,
environment variables, then command-line options. Credentials must be supplied through
environment variables or an external secret store; do not commit them to a configuration
file. See [`.env.example`](.env.example) for supported environment variable names.

`doctor` performs a short local Zotero probe by default and reports each missing or
unreachable dependency independently. It never prints secret values. Use
`--skip-zotero-check` when diagnosing configuration away from the desktop machine.

## Local Zotero synchronization

After `doctor` confirms that Zotero's Local API is reachable, run an initial local sync:

```bash
uv run zotero-arxiv-daily profile sync
```

The default database is `runtime/zotero.sqlite3`, which is ignored by Git. The command
fetches the complete library on its first run and uses the last successful library version
for later incremental runs. It prints only counts, never notes, annotations, or bibliographic
content. Override the storage location with `--database PATH` or `ZAD_LOCAL_DATABASE_PATH`.
Stop or retry an interrupted sync normally: the SQLite transaction retains the previous usable
state until the new batch is valid and complete.

Build a compact local remote-profile candidate after synchronization:

```bash
uv run zotero-arxiv-daily profile build
```

The v0.2 profile separates weak library metadata from stronger manual tags, collection membership,
annotations, and optional curated positive examples. It uses local time decay for recent interests
and derives bounded domain, method, and task facets. The protected v4 export contains only those
derived facets and allowlisted topics; it never contains collection names or keys, titles, notes,
annotations, identifiers, or labels. Use `--corpus-state PATH` to use a non-default local curated
ledger; an absent ledger simply contributes no curated evidence.

Optional watched identities are structured configuration, not global defaults. Exact normalized
names and aliases can add a bounded local score component; watchlists stay in the protected profile
and are not sent to DeepSeek. For example:

```toml
[[watched_authors]]
name = "Yann LeCun"

[[watched_authors]]
name = "Fei-Fei Li"
aliases = ["Li Fei-Fei"]

[[watched_authors]]
name = "Saining Xie"

[[watched_institutions]]
name = "DeepMind"
aliases = ["Google DeepMind"]

[[watched_institutions]]
name = "Meta"
aliases = ["Meta AI", "FAIR"]

[[watched_institutions]]
name = "OpenAI"

[[watched_institutions]]
name = "ByteDance"

[[watched_institutions]]
name = "Carnegie Mellon University"
aliases = ["CMU"]

[[watched_institutions]]
name = "Massachusetts Institute of Technology"
aliases = ["MIT"]

[[watched_institutions]]
name = "Stanford University"
aliases = ["Stanford"]
```

At most 32 authors and 32 institutions are accepted, with at most 8 aliases per identity and 160
UTF-8 bytes per value. Exact equality after Unicode, case, punctuation, and whitespace normalization
is required; substring matching and author disambiguation are intentionally not performed. Missing
arXiv affiliation metadata gives no institution bonus. Defaults are `0.75` for an author, `0.5` for
an institution, and `1.0` combined, configurable through the variables in `.env.example`.

It writes `runtime/remote-profile.json` with owner-only permissions. The export contains only
bounded topic terms and inferred arXiv categories; it excludes titles, abstracts, notes,
annotations, identifiers, collections, and matching evidence. Unchanged local inputs reuse
derived digest cache entries rather than regenerating them. URL/domain fragments and reserved
`zad:`/`ranking-reason:` feedback tags are excluded from interest terms, so review labels cannot
silently become recommendation topics.

## Local curated evaluation corpus

The optional curated corpus is a local-only, evolving source of explicit judgments. It is not sent
to GitHub, the static site, or the model provider. First synchronize Zotero, then list local
collection keys and map one or more collections to positive and hard-negative labels:

```bash
uv run zotero-arxiv-daily corpus list-collections
uv run zotero-arxiv-daily corpus import-zotero \
  --positive-collection POSITIVE_COLLECTION_KEY \
  --negative-collection NEGATIVE_COLLECTION_KEY
```

The importer writes the ignored, owner-only `runtime/curated-corpus.json` ledger. Re-running it is
idempotent. Moving an item between mapped collections creates a correction event; removing it from
all mapped collections creates an explicit `unlabeled` event, never a negative label. A Zotero DOI
is normalized automatically. For an arXiv paper without a DOI, add a manual tag such as
`ranking-paper-id:arxiv:2401.00001`. Optional structured reason tags use the form
`ranking-reason:novel-insight`; keep free-text rationale in Zotero or another ignored local file.

Every offline evaluation freezes a separate immutable snapshot with the corpus digest, cutoff,
stable-anchor, rolling, temporal, and pairwise paper identities. Sparse or non-overlapping samples
remain explicit uncertainty-bearing reference results: they cannot authorize automatic tuning, while
an operator may explicitly review a reversible canary.

## Optional public evidence enrichment

The optional evidence command enriches only the bounded public candidate pool. It sends an exact
public DOI from the arXiv Atom record to OpenAlex; it never sends a Zotero record, profile facet,
feedback event, collection name, note, annotation, or free-text rationale. Candidates without a DOI,
unmatched records, provider errors, and rate limits remain explicit `unknown` evidence rather than a
negative quality signal.

```bash
uv run zotero-arxiv-daily evidence enrich --limit 40
```

The command writes the ignored, owner-only `runtime/openalex-evidence.json` TTL cache. It is not part
of the default recommendation workflow yet, so a provider outage cannot prevent a daily batch from
using the previous usable output. OpenAlex context is restricted to citation/reference counts,
open-access state, and retraction state; it is a weak contextual input, never a direct quality score.

The default refined path separately checks a bounded set of approved HTTPS project-page URLs that are
explicitly present in each public abstract. It follows only revalidated approved redirects, caches the
result for one day, and does not retain page content. A reachable page is a positive open-source proxy,
not proof that code or a license exists. Missing, rejected, unreachable, or timed-out pages never
penalize a paper or block the batch.

## Feedback collection

Browser feedback is imported as local append-only per-paper events on every scheduled run. Successful
publication impressions are stored with display positions so later versions can evaluate explicit
outcomes without treating silence as a negative label. v0.2.0 does not convert these events into
scores, prompts, profile terms, or weight changes. No feedback prose is published or sent to the model.

## Ranking evaluation and refinement

The local ranker uses an immutable, versioned weight-set registry at
`runtime/ranking-weights.json`. The daily path creates the conservative `coarse-v1` definition if
the registry is absent. Coarse screening consumes only interest, recency, and exact watched-author or
watched-institution features. Fine ranking starts from that score and adds uncertainty-discounted
`judge-v3` abstract quality plus validated project-page availability. Feedback is not a ranking input.

```bash
uv run zotero-arxiv-daily ranking weights
```

Freeze the evolving local corpus before comparing the normalized ranker with the v0.1.2 baseline:

```bash
uv run zotero-arxiv-daily evaluate snapshot
uv run zotero-arxiv-daily evaluate shadow \
  --profile runtime/remote-profile.json \
  --candidate-state runtime/arxiv-state.json \
  --snapshot-id SNAPSHOT_ID
```

The ignored `runtime/shadow-report.json` contains aggregate metrics and feature-group ablations only.
It reports raw corpus Recall, candidate-label overlap, and candidate-conditional Recall separately.
These provisional metrics are observations for v0.2.1 and later; NDCG, negative-label rate, Recall,
latency, token use, and cost do not approve or block v0.2.0. Shadow evaluation never changes feedback
state or publishes a batch. Weight activation is an explicit reversible operator action:

```bash
uv run zotero-arxiv-daily ranking activate-weights \
  --version coarse-v2
```

`ZAD_LLM_REFINEMENT_ENABLED` is enabled in the production workflow and `.env.example` so quality
assessment is the default path. The pipeline judges only the coarse shortlist with `judge-v3`,
applies local selection, and asks `explain-v2` for only the final papers. The judge uses fixed score
anchors and uncertainty; explanations must name the problem, approach, claimed result, a concrete
paper-specific contribution, and a limitation grounded in at least two supplied fields. Existing
older judge/explain cache entries are intentionally ignored. `ZAD_LLM_PREFERENCE_CONTEXT_APPROVED`
is a separate opt-in for fixed categorical relevance signals such as `topic_overlap`; it never
sends terms, notes, annotations, collection names, labels, or feedback prose. Do not set it without
documenting the field-level trust-boundary approval. Batch size, request token/byte limits, retry
count, request count, and provider output tokens are bounded by the `ZAD_LLM_*` settings in
`.env.example`.

The explicit non-refinement fallback uses `proposal-v2` with the same factual grounding rules and a
new cache namespace. It is not a v0.1.2 rollback. For a production rollback rehearsal, manually run
the daily workflow with `use_v012_ranking=true`; this applies the frozen v0.1.2 scoring, quotas,
diversity, proposal prompt, and final ordering while retaining the current encrypted state and Pages
protocols. Confirm `weight_set_version` is `v0.1.2`, then restore production with another manual run
using the default `use_v012_ranking=false` and confirm `weight_set_version` is `coarse-v1`.

To make a real held-out overlap measurable, hydrate an evaluation-only candidate state from exact
identities in a frozen snapshot. This never adds labeled papers to production retrieval:

```bash
uv run zotero-arxiv-daily evaluate hydrate-candidates \
  --snapshot-id SNAPSHOT_ID \
  --candidate-state runtime/evaluation-candidates.json
```

Provider usage metadata is useful for an efficiency observation but is not a quality gate. Record
the privacy-safe manifest after each run, then compare equal-model baseline and candidate histories:

```bash
uv run zotero-arxiv-daily evaluate record-manifest
uv run zotero-arxiv-daily evaluate efficiency \
  --baseline runtime/baseline-manifests.json \
  --candidate runtime/run-manifest-history.json \
  --output runtime/efficiency-report.json
```

The comparison uses median input/output tokens per deployed recommendation, requests, cache hits,
provider latency, duration, and measured cost. It is an observational report while quality is being
improved; a missing usage value or a higher token count does not block a quality canary or deployment.

Manual production acceptance can capture one private manifest as the efficiency baseline and compare
a later candidate run without exporting protected state. The workflow summary exposes only the
allowlisted aggregate manifest and comparison fields. A separate manual-only failure-injection input
stops after Pages deployment so that the next run can exercise reconciliation; it must remain false
outside that controlled rehearsal.

To publish a validated exported profile to a GitHub Actions Secret, authenticate `gh` locally and
set `ZAD_GITHUB_REPOSITORY`; the profile JSON is sent on standard input rather than in command-line
arguments:

```bash
uv run zotero-arxiv-daily profile publish-github
```

## Current status and operation

The production workflow is scheduled at `10:30 UTC` (`18:30 Asia/Shanghai`). It evaluates actual
local time before generation: delayed scheduled runs outside `18:30–08:30` skip without a model
call or state update. A manual peak-time run must explicitly set `allow_peak_generation=true`.
DeepSeek's current pricing page does not advertise the historical off-peak discount, so this window
is a user-approved cost-control policy rather than a claim of discounted pricing.

Build static output from validated publishable recommendations with the default protected mode:

```bash
export ZAD_PAGES_PASSPHRASE='use-a-strong-passphrase-of-at-least-16-characters'
uv run zotero-arxiv-daily site build
```

The builder expects `runtime/publishable-recommendations.json`, creates encrypted static data in
`runtime/site`, and prompts for the passphrase only in the browser. To knowingly make generated
recommendations public, set `ZAD_PUBLIC_OUTPUT=true` and leave `ZAD_PAGES_PASSPHRASE` unset.
The site interface and default generated summaries/reasons are English. The generated-prose
language remains configurable with `ZAD_OUTPUT_LANGUAGE`, while interface controls remain English
to avoid mixed-language navigation. The site stores feedback only in browser local storage and
opens one prefilled GitHub Issue after an explicit user action; it contains no browser token. Raw
Zotero content stays in the ignored local SQLite database.

Published batches display generation start/completion, artifact build time, a meaningful profile
snapshot, and a validated link to the successful GitHub workflow run in `Asia/Shanghai`. Data older than 36
hours is marked stale. Successfully deployed canonical arXiv IDs are suppressed for 14 days by
default. Public candidate metadata is retained for 30 days in a bounded 1,000-paper pool, allowing
an empty incremental retrieval to fall back to recent papers that are absent from successful-
publication history. An empty legacy pool receives one bounded seven-day backfill. History is
prepared during generation and promoted to the protected `state` branch only after Pages deployment
succeeds. Existing v0.1.0 profiles, arXiv state, and publishable payloads remain readable; rebuilding
and republishing the profile activates schema-v2 watchlists.

The protected workflow state is stored as an AES-GCM bundle in `state.enc.json`, using the separate
`ZAD_STATE_ENCRYPTION_KEY` GitHub Secret. It must not reuse `ZAD_PAGES_PASSPHRASE`; the state key is
never sent to the browser. A legacy plaintext state branch is migrated once and then rejected if it
cannot be validated or decrypted. After a verified migration, a repository administrator must run a
manual workflow with `purge_legacy_state_history=true`; it replaces the `state` branch with a
single encrypted-root commit using `--force-with-lease`. Confirm the protected encrypted backup and
the absence of raw branch URLs before this irreversible history purge.

When retrieval uses a recent validated snapshot after bounded failure, the private run manifest records
the degraded reason and source checkpoint, and the published site marks the candidate pool as degraded.

Publishable site schema v4 adds an optional model-generated limitation/uncertainty note. Readers
retain exact v1-v3 adapters and omit the field when it is unavailable.

The `Profile snapshot` is the time of the successful local Zotero synchronization used to build
the protected profile. Rebuild and republish the profile after upgrading to v0.2.0 to populate the
v4 weighted facets; legacy protected profiles remain valid and use explicit unavailable defaults.

Within a generated batch, cards are ordered by local profile relevance, then validated model quality,
then the latest arXiv revision time and canonical ID. Candidate quotas and diversity constraints still
determine which papers enter the batch; they do not group the reading order.

## Quality checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
npm ci
npx playwright install chromium
npm run test:e2e
uv build
python scripts/check_artifacts.py
```

The checks use only synthetic fixtures and run offline by default.

## License

This project is licensed under the [MIT License](LICENSE).
