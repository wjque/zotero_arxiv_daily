# ADR-0011: Expected-Worthwhile Selection

- **Status:** Accepted
- **Date:** 2026-08-30

## Context

The v0.3.0 product metric is the number of papers the user explicitly marks worthwhile after
reading. Selection through v0.2.1 optimizes a single blended relevance score in which personal
interest and scientific quality are already fused, so the resulting batch can only be reviewed as
one number. Two distinct failures - recommending a familiar paper that turns out to be weak, and
skipping an unfamiliar paper that would have been valuable - are indistinguishable in that score
and therefore cannot be corrected separately.

An objective built on the collected feedback would be the obvious alternative and is the wrong one
here. Post-reading outcomes are sparse, self-selected, and submitted at the reader's convenience,
so fitting a policy to them would encode reporting habits as preferences and would make silence
look like rejection.

## Decision

Selection gains a second declared objective, `expected_worthwhile`, defined as
`P(read | shown) x P(worthwhile | read)`. Both factors are estimated separately and stay separately
inspectable on every candidate, together with the confidence behind each.

Each factor is produced by an affine map into a declared interval followed by shrinkage toward a
declared prior: `mapped = floor + (ceiling - floor) * raw`, then
`calibrated = prior + (mapped - prior) * confidence`. Because `floor <= prior <= ceiling`, the
result is bounded and monotone in the evidence, and zero confidence returns the prior rather than
zero. Reading likelihood draws on the interest, recency, and identity feature groups; post-reading
value draws on the scientific-quality and reproducibility groups plus any local scientific-value
assessment. Group importance reuses the active ranking weight set instead of a second undeclared
constant set. Confidence is discounted by evidence coverage, so absent evidence lowers certainty
and never lowers the value; an assessment that was never produced does not count as missing.

`DEFAULT_WORTHWHILE_POLICY` is version `declared-prior-v1`: reading `(0.20, 0.90)` around a prior of
`0.35`, and post-reading value `(0.10, 0.85)` around a prior of `0.30`. The non-zero reading floor
is deliberate. A shown paper is already in front of the reader, so a paper with no interest evidence
must not collapse to an expected value of zero under a product objective.

The objective decides only the order in which the qualified pool is walked. Minimum score, judged
quality, confident scientific-value rejection, source quotas, author and topic diversity, and the
batch target apply identically under both objectives, and ties break on canonical arXiv ID. A
candidate with no estimate falls back to the declared no-evidence prior.

`RELEVANCE` remains the default. The pipeline and all three workflows are untouched, so the
scheduled daily path stays exactly on the released v0.2.1 objective. The new objective is reachable
only through offline evaluation and the `evaluate worthwhile` command.

Offline outcome evaluation lives in `evaluation/worthwhile.py`, which imports both `feedback` and
`ranking` so that `ranking` never imports `feedback`. Feedback can therefore inform a human review
of the declared policy but cannot become a ranking input. The report may propose a calibration
derived from observed rates; proposing it activates nothing.

## Consequences

- Interest and scientific value can now regress independently and be diagnosed independently.
- Conservative priors and coverage discounting keep low-evidence papers near the prior, so the
  objective separates candidates less sharply than relevance does. That is intended: a confident
  ordering is not available from the evidence actually on hand.
- Batch composition under the new objective can differ substantially from relevance ordering. It is
  not exercised in production and carries no measured outcome improvement yet.
- The observed reading rate counts only reported reads and is a lower bound on `P(read | shown)`. A
  proposed reading prior is therefore never taken below the declared floor.
- Two policies now exist without a persisted registry. Versioned activation is V030-M6's scope; the
  declared policy version is recorded in every report so a later comparison stays attributable.

## Migration and Rollback

No persisted schema changes. `runtime/worthwhile-report.json` is ignored local output, is written
with owner-only permissions, and is deliberately outside the encrypted state allowlist, so it is
never restored, published, or migrated. Rollback is deleting that file and ignoring the new
objective: nothing reads it, and `SelectionPolicy()` continues to produce the released v0.2.1
selection byte for byte.
