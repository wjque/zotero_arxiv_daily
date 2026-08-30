# ADR-0012: Uncertainty-Targeted Bounded Exploration

- **Status:** Accepted
- **Date:** 2026-08-30

## Context

Selection through v0.2.1 already reserves a small off-category quota, and calling that quota
exploration conflates two different things. An off-category paper the ranker has already scored
confidently is not a risk, it is simply a paper from a different category; reading it confirms what
the estimates already said. A paper about which nothing at all is known has the widest possible
range of outcomes, but that range comes from absence of evidence rather than from a question the
ranker could resolve, so spending a slot on it is noise, not information.

V030-M4 made the missing distinction available. The declared objective now reports reading
likelihood and post-reading value separately, each with the confidence of the evidence behind it,
so it is possible to ask how far a specific estimate could still move and why.

A batch also has a fixed target. Any exploration slot displaces a paper the objective ranked higher,
which means exploration always has a price measured in the same unit as the product metric. Without
a declared bound on that price, exploration is an unpriced claim on the reader's attention.

## Decision

Exploration targets resolvable uncertainty about worthwhileness, and it is a constraint on cost
rather than a bonus added to a score.

Each factor's calibrated estimate is widened by whatever evidence has not yet resolved:
`low = value - (1 - confidence) * (value - floor)` and
`high = value + (1 - confidence) * (ceiling - value)`, against the same reviewed
`declared-prior-v1` calibration ADR-0011 established. Full confidence collapses the interval onto
the estimate, and no confidence opens it to the whole declared interval, so a widened bound can
never leave the reviewed range. From the two intervals come `potential = reading_high x value_high`,
`conservative = reading_low x value_low`, and `uncertainty = potential - conservative`.

A candidate is eligible for an exploration slot only when all of the following hold, under
`bounded-uncertainty-v1`:

- Post-reading-value evidence exists at all, and its confidence is at least `0.25`. A paper nothing
  is known about is refused as `no_value_evidence`, not admitted for having a wide interval.
- `potential` is at least `0.35`, so the optimistic end is genuinely promising.
- `uncertainty` is at least `0.25`, so there is still something to learn. A settled estimate is
  refused as `uncertainty_below_minimum` regardless of its category.
- The estimate came from the same declared objective policy version, and the paper is not in the
  publication-history suppression window.

Cost is `max(best_expected_in_pool - expected, 0)`, and admitted costs must sum to at most the
declared risk budget of `0.20` expected worthwhile reads. The truly displaced candidate is the
marginal one rather than the pool maximum, so this over-states the price and the budget binds
conservatively. The default budget is one paper per batch, so a bad exploration day costs one slot.

Candidates are ordered by `potential` quantized to two decimals, and candidates that tie in that
bucket are rotated by `blake2b(seed || arxiv_id)`. A fixed seed therefore reproduces a batch
exactly, while a different seed can reach a different member of an equivalent set rather than
repeating one paper forever.

Eligibility is not restated here. `qualified_candidates` in `ranking/select.py` is now the single
definition of what a batch may contain, and both ordinary selection and exploration read the pool
through it, so exploration structurally cannot reach a paper minimum score, judged quality, or a
confident scientific-value rejection excluded. A supplied decision reserves only slots it has
already paid for, its picks are looked up in the qualified pool, and a pick that is absent is
dropped rather than forced. When a decision is supplied, it also owns the whole off-category quota,
so a batch cannot hold more off-category papers than the declared budget under either reading of the
word.

Every decision records what it spent, how many candidates it considered, how many were eligible, the
declared bounds behind each admitted candidate, and the distinct reasons everything else was
refused.

As with V030-M4, nothing here is wired into the pipeline or the workflows. Exploration is reachable
only where the objective is, and activation remains V030-M6's scope.

## Consequences

- Exploration now has a price in the product metric, and the budget makes that price reviewable
  before a batch is published rather than after.
- Exploration will frequently decline. A pool of settled estimates yields no eligible candidate, and
  the slot returns to ordinary selection instead of being filled for its own sake.
- Because eligibility requires existing value evidence, exploration cannot reach genuinely novel
  areas the local evidence pipeline never assesses. That is the intended trade: unassessed papers
  are outside the declared risk model, not inside it at maximum uncertainty.
- The over-stated cost means some genuinely affordable exploration is refused as too expensive. A
  refusal is recorded as `cost_exceeds_risk_budget`, so the conservatism is visible rather than
  silent.
- Two decimals of `potential` define equivalence. Candidates whose potential differs in the third
  decimal are ordered strictly, so the seed rotates among genuinely comparable candidates only.

## Migration and Rollback

No persisted schema changed, and no workflow file was modified. `ExplorationDecision` is passed
explicitly, defaults to `None`, and `select_diverse` without one behaves exactly as before, so the
scheduled daily path continues to produce the released v0.2.1 selection. Rollback is not passing a
decision.
