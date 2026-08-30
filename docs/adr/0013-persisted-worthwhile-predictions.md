# ADR-0013: Persisted Per-Batch Worthwhile Predictions

- **Status:** Accepted
- **Date:** 2026-08-30

## Context

ADR-0011 declared the worthwhile objective and ADR-0012 declared what exploration may spend against
it. Both produce numbers at publication time, and both discard them immediately. The offline report
in `evaluation/worthwhile.py` has accepted a `predictions` mapping since V030-M4, but no caller has
ever supplied one, so every report emitted so far carries the warning *"no batch prediction was
supplied; realized outcomes are reported alone"*.

That leaves the objective unfalsifiable. `declared-prior-v1` says a shown paper is read with
probability at least `0.20`, and `bounded-uncertainty-v1` says an exploration slot costs at most
`0.20` expected worthwhile reads. Nothing in the system can currently say whether either claim
survived contact with what the reader actually did, because the prediction half of the comparison
was never written down. V030-M6 requires a policy comparison against explicit post-reading outcomes;
that comparison cannot exist until the prediction is durable.

The batch identity is the hard part. `feedback record-impressions` derives `published-{started}` from
a deployed set, and the same string is reconstructed independently by the deployment receipt in
`daily.yml` and by the browser in `site/build.py`. A prediction filed under a slightly different key
does not fail — the report simply finds no prediction for that batch and reports realized outcomes
alone, which is indistinguishable from not having recorded anything at all.

## Decision

Predictions for a published batch are persisted locally at publication time, in
`worthwhile-predictions.json`, and read only by the offline report.

`published_batch_id` in `site/models.py` becomes the single Python definition of the impression
batch identity. Both `feedback record-impressions` and `recommend run` call it, and `recommend run`
calls it on the very `PublishedRecommendationSet` instance it writes to disk, so the prediction key
and the impression key are derived from one object and cannot drift. The workflow and the browser
keep their own copies of the format because neither can import Python; a test pins all three
together.

`run_refined_recommendation` returns the estimates alongside its result and manifest. They are
computed by `estimate_worthwhile` *after* `select_diverse` has run and are never read back into it,
so ranking is unchanged by construction rather than by inspection. They are ordered to match the
published records, which is the order impressions receive displayed ranks in, and are kept 1:1 with
them: a record the estimator did not reach takes the declared no-evidence estimate from ADR-0011
rather than dropping out and silently shortening the batch.

The store is schema-versioned at `1` and is keyed by batch. Re-recording an identical batch is a
no-op so a retried publication is safe; a conflicting record for the same batch is refused rather
than overwritten, because the stored prediction must remain the one that was actually served. To
make that idempotence real, a batch is timestamped by its own generation instant — the same instant
its batch ID encodes — rather than by the wall clock at write time. Retention keeps the newest sixty
batches, which is twice the horizon the report's thirty-outcome sufficiency threshold can consume.

Only the refined path records. `run_recommendation` and the frozen v0.1.2
`run_baseline_recommendation` rollback path are untouched and write nothing.

The report gains one warning. Sums across a history that predates this change would otherwise
compare a predicted total covering some batches against realized counts covering all of them, so a
partially covered history now says so explicitly instead of reporting a misleadingly low prediction.

## Consequences

- The objective becomes falsifiable. A declared constant that consistently over- or under-states
  realized outcomes is now visible in the report, which is the input V030-M6 needs.
- The file records canonical arXiv IDs and normalized scores. That is the same class of data as the
  impressions already in `feedback-state.json`, it lives inside the same AES-GCM state bundle, and
  it is never published, never in the site payload, and never in a model request.
- A batch that is predicted but never deployed leaves an orphan entry. The report iterates ledger
  batches, so an orphan is never looked up and never enters a total; retention bounds its cost.
- This is still observation only. Nothing here adopts a proposed calibration, and
  `eligible_for_activation` remains `False` pending an explicit operator decision.
- Recording only the refined path means a v0.1.2 rollback run produces no prediction. That is
  intended: the rollback path exists to reproduce released behavior exactly, and adding a write to
  it would weaken that guarantee for no analytical gain.

## Migration and Rollback

`worthwhile-predictions.json` is a new optional state file. The feedback ledger stays at schema v2
and no existing persisted schema changed, so a state bundle written before this change decrypts and
restores unchanged. An absent file yields an empty mapping, which restores the previous report
behavior exactly, including the "no batch prediction was supplied" warning.

Rollback is deleting the file, or passing `--predictions` at a path that does not exist. Neither
affects ranking, selection, the published site, or the scheduled workflow's output.
