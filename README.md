# Zotero arXiv Daily

Zotero arXiv Daily is a local-first tool that builds a compact interest profile from a
Zotero library and uses it to produce a daily arXiv reading list. Raw Zotero records,
notes, annotations, and PDF content remain local.

The v0.1.2 release adds a meaningful profile snapshot timestamp, compact batch status, and
deterministic recommendation ordering. Current work is tracked in the active
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
derived digest cache entries rather than regenerating them.

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
stable-anchor, rolling, temporal, and pairwise paper identities. Fewer than 40 independent labels
is reported as provisional and cannot approve automatic tuning.

## Feedback activation cadence

Browser feedback is imported as local append-only events. It can be collected on every scheduled
run, while ranking adjustments activate only after a guarded weekly evaluation (seven days by
default). The previous successful snapshot remains active after empty, insufficient, or failed
weeks. `ZAD_FEEDBACK_ACTIVATION_INTERVAL_DAYS` and `ZAD_FEEDBACK_MINIMUM_INDEPENDENT_PAPERS` adjust
the local operational bounds; no feedback prose is sent through GitHub Issues or published output.

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

The `Profile snapshot` is the time of the successful local Zotero synchronization used to build
the protected profile. Rebuild and republish the profile after upgrading to v0.1.2 to populate this
field; legacy protected profiles remain valid but omit it.

Within a generated batch, cards are ordered by local profile relevance, then validated model quality,
then the latest arXiv revision time and canonical ID. Candidate quotas and diversity constraints still
determine which papers enter the batch; they do not group the reading order.

## Quality checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
python scripts/check_artifacts.py
```

The checks use only synthetic fixtures and run offline by default.

## License

This project is licensed under the [MIT License](LICENSE).
