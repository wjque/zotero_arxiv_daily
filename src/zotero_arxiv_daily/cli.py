"""Thin command-line entry point for currently implemented use cases."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily import __version__
from zotero_arxiv_daily.arxiv.categories import expand_one_hop
from zotero_arxiv_daily.arxiv.client import ArxivClient
from zotero_arxiv_daily.arxiv.models import RetrievalCheckpoint
from zotero_arxiv_daily.arxiv.retrieval import retrieve
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.config import load_config
from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.core.time import generation_decision
from zotero_arxiv_daily.doctor import Diagnostic, doctor_exit_code, run_doctor
from zotero_arxiv_daily.evaluation.calibration import run_shadow_evaluation, write_shadow_report
from zotero_arxiv_daily.evaluation.candidates import hydrate_labeled_candidates
from zotero_arxiv_daily.evaluation.corpus import (
    CorpusStore,
    CuratedCorpusMapping,
    ZoteroCorpusItem,
)
from zotero_arxiv_daily.evaluation.efficiency import compare_manifest_files, record_manifest
from zotero_arxiv_daily.evaluation.offline import (
    EvaluationSnapshotStore,
    make_evaluation_snapshot,
)
from zotero_arxiv_daily.evidence.openalex import OpenAlexClient, OpenAlexEvidenceEnricher
from zotero_arxiv_daily.evidence.storage import EvidenceCache
from zotero_arxiv_daily.feedback.ingest import FeedbackStateStore, read_github_issues
from zotero_arxiv_daily.feedback.ledger import FeedbackLedgerStore
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.deepseek import DeepSeekClient
from zotero_arxiv_daily.pipeline.recommend import run_recommendation, run_refined_recommendation
from zotero_arxiv_daily.profile.export import write_remote_profile
from zotero_arxiv_daily.profile.models import WatchedIdentity
from zotero_arxiv_daily.profile.service import (
    build_cached_remote_profile,
    local_curated_item_keys,
    publish_github_secret,
    read_remote_profile,
)
from zotero_arxiv_daily.ranking.weights import DEFAULT_WEIGHT_SET, WeightSet, WeightSetRegistry
from zotero_arxiv_daily.security.state import decrypt_state_bundle, encrypt_state_directory
from zotero_arxiv_daily.site.build import build_site
from zotero_arxiv_daily.site.models import (
    WorkflowRun,
    make_published_set,
    read_published_set,
    write_published_set,
)
from zotero_arxiv_daily.storage.recommendation_history import RecommendationHistoryStore
from zotero_arxiv_daily.zotero.client import ZoteroLocalClient
from zotero_arxiv_daily.zotero.storage import ZoteroStore
from zotero_arxiv_daily.zotero.sync import synchronize


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser without performing configuration or network work."""

    parser = argparse.ArgumentParser(prog="zotero-arxiv-daily")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="Path to a TOML or JSON configuration file")
    parser.add_argument("--zotero-base-url", help="Override the local Zotero API base URL")
    subcommands = parser.add_subparsers(dest="command", required=True)
    schedule_parser = subcommands.add_parser("schedule", help="Evaluate the model-cost window")
    schedule_parser.add_argument(
        "--event-name", choices=("schedule", "workflow_dispatch"), required=True
    )
    schedule_parser.add_argument("--allow-peak-generation", action="store_true")
    doctor_parser = subcommands.add_parser(
        "doctor", help="Diagnose local and protected dependencies"
    )
    doctor_parser.add_argument(
        "--skip-zotero-check", action="store_true", help="Do not contact the local Zotero API"
    )
    doctor_parser.add_argument("--format", choices=("text", "json"), default="text")
    profile_parser = subcommands.add_parser(
        "profile", help="Manage the local interest-profile source"
    )
    profile_commands = profile_parser.add_subparsers(dest="profile_command", required=True)
    sync_parser = profile_commands.add_parser("sync", help="Synchronize the local Zotero library")
    sync_parser.add_argument(
        "--database", type=Path, help="Override the local SQLite database path"
    )
    sync_parser.add_argument("--format", choices=("text", "json"), default="text")
    build_parser = profile_commands.add_parser("build", help="Build a local interest profile")
    build_parser.add_argument(
        "--database", type=Path, help="Override the local SQLite database path"
    )
    build_parser.add_argument("--output", type=Path, default=Path("runtime/remote-profile.json"))
    build_parser.add_argument("--payload-budget", type=int, default=30 * 1024)
    build_parser.add_argument(
        "--corpus-state", type=Path, default=Path("runtime/curated-corpus.json")
    )
    publish_parser = profile_commands.add_parser(
        "publish-github", help="Publish a protected profile through gh"
    )
    publish_parser.add_argument("--input", type=Path, default=Path("runtime/remote-profile.json"))
    publish_parser.add_argument("--secret-name", default="ZOTERO_ARXIV_DAILY_PROFILE")
    site_parser = subcommands.add_parser("site", help="Build the static recommendation site")
    site_commands = site_parser.add_subparsers(dest="site_command", required=True)
    site_build_parser = site_commands.add_parser(
        "build", help="Build encrypted or public static output"
    )
    site_build_parser.add_argument(
        "--input", type=Path, default=Path("runtime/publishable-recommendations.json")
    )
    site_build_parser.add_argument("--output", type=Path, default=Path("runtime/site"))
    site_build_parser.add_argument(
        "--candidate-state", type=Path, default=Path("runtime/arxiv-state.json")
    )
    feedback_parser = subcommands.add_parser("feedback", help="Ingest validated browser feedback")
    feedback_commands = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_ingest_parser = feedback_commands.add_parser(
        "ingest", help="Ingest a JSON projection of GitHub Issues"
    )
    feedback_ingest_parser.add_argument("--input", type=Path, required=True)
    feedback_ingest_parser.add_argument(
        "--state", type=Path, default=Path("runtime/feedback-state.json")
    )
    feedback_activate_parser = feedback_commands.add_parser(
        "activate", help="Atomically evaluate the next eligible weekly feedback snapshot"
    )
    feedback_activate_parser.add_argument(
        "--state", type=Path, default=Path("runtime/feedback-state.json")
    )
    feedback_impressions_parser = feedback_commands.add_parser(
        "record-impressions", help="Record successful publication exposure locally"
    )
    feedback_impressions_parser.add_argument("--input", type=Path, required=True)
    feedback_impressions_parser.add_argument(
        "--state", type=Path, default=Path("runtime/feedback-state.json")
    )
    corpus_parser = subcommands.add_parser(
        "corpus", help="Import a local curated Zotero collection into the evaluation ledger"
    )
    corpus_commands = corpus_parser.add_subparsers(dest="corpus_command", required=True)
    corpus_list_parser = corpus_commands.add_parser(
        "list-collections", help="List local collection keys for an explicit corpus mapping"
    )
    corpus_list_parser.add_argument("--database", type=Path)
    corpus_import_parser = corpus_commands.add_parser(
        "import-zotero", help="Import explicit positive and negative collection keys"
    )
    corpus_import_parser.add_argument("--database", type=Path)
    corpus_import_parser.add_argument("--positive-collection", action="append", required=True)
    corpus_import_parser.add_argument("--negative-collection", action="append", required=True)
    corpus_import_parser.add_argument(
        "--state", type=Path, default=Path("runtime/curated-corpus.json")
    )
    evidence_parser = subcommands.add_parser(
        "evidence", help="Enrich bounded public DOI metadata without local-profile data"
    )
    evidence_commands = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    evidence_enrich_parser = evidence_commands.add_parser(
        "enrich", help="Cache optional OpenAlex context for the top public candidates"
    )
    evidence_enrich_parser.add_argument(
        "--candidate-state", type=Path, default=Path("runtime/arxiv-state.json")
    )
    evidence_enrich_parser.add_argument(
        "--cache", type=Path, default=Path("runtime/openalex-evidence.json")
    )
    evidence_enrich_parser.add_argument("--limit", type=int, default=40)
    evaluate_parser = subcommands.add_parser(
        "evaluate", help="Create immutable local snapshots and non-mutating ranking shadow reports"
    )
    evaluate_commands = evaluate_parser.add_subparsers(dest="evaluate_command", required=True)
    evaluate_snapshot_parser = evaluate_commands.add_parser(
        "snapshot", help="Freeze the current local curated corpus for an offline replay"
    )
    evaluate_snapshot_parser.add_argument(
        "--corpus-state", type=Path, default=Path("runtime/curated-corpus.json")
    )
    evaluate_snapshot_parser.add_argument(
        "--snapshots", type=Path, default=Path("runtime/evaluation-snapshots")
    )
    evaluate_snapshot_parser.add_argument("--anchor-paper-id", action="append", default=[])
    evaluate_snapshot_parser.add_argument("--rolling-days", type=int, default=30)
    evaluate_shadow_parser = evaluate_commands.add_parser(
        "shadow", help="Compare a candidate weight set with the frozen v0.1.2 ranker"
    )
    evaluate_shadow_parser.add_argument("--profile", type=Path, required=True)
    evaluate_shadow_parser.add_argument("--candidate-state", type=Path, required=True)
    evaluate_shadow_parser.add_argument(
        "--feedback-state", type=Path, default=Path("runtime/feedback-state.json")
    )
    evaluate_shadow_parser.add_argument("--snapshot-id", required=True)
    evaluate_shadow_parser.add_argument(
        "--snapshots", type=Path, default=Path("runtime/evaluation-snapshots")
    )
    evaluate_shadow_parser.add_argument("--weight-state", type=Path)
    evaluate_shadow_parser.add_argument(
        "--output", type=Path, default=Path("runtime/shadow-report.json")
    )
    evaluate_hydrate_parser = evaluate_commands.add_parser(
        "hydrate-candidates",
        help="Fetch exact public arXiv identities from a frozen evaluation snapshot",
    )
    evaluate_hydrate_parser.add_argument("--snapshot-id", required=True)
    evaluate_hydrate_parser.add_argument(
        "--snapshots", type=Path, default=Path("runtime/evaluation-snapshots")
    )
    evaluate_hydrate_parser.add_argument("--candidate-state", type=Path, required=True)
    evaluate_hydrate_parser.add_argument("--batch-size", type=int, default=20)
    evaluate_record_manifest_parser = evaluate_commands.add_parser(
        "record-manifest", help="Append one privacy-safe run manifest to local history"
    )
    evaluate_record_manifest_parser.add_argument(
        "--input", type=Path, default=Path("runtime/run-manifest.json")
    )
    evaluate_record_manifest_parser.add_argument(
        "--history", type=Path, default=Path("runtime/run-manifest-history.json")
    )
    evaluate_efficiency_parser = evaluate_commands.add_parser(
        "efficiency", help="Compare measured baseline and candidate run manifests"
    )
    evaluate_efficiency_parser.add_argument("--baseline", type=Path, required=True)
    evaluate_efficiency_parser.add_argument("--candidate", type=Path, required=True)
    evaluate_efficiency_parser.add_argument(
        "--output", type=Path, default=Path("runtime/efficiency-report.json")
    )
    state_parser = subcommands.add_parser(
        "state", help="Encrypt and restore private workflow state"
    )
    state_commands = state_parser.add_subparsers(dest="state_command", required=True)
    state_encrypt_parser = state_commands.add_parser(
        "encrypt", help="Encrypt validated state files into one bundle"
    )
    state_encrypt_parser.add_argument("--input-dir", type=Path, default=Path("runtime"))
    state_encrypt_parser.add_argument("--output", type=Path, default=Path("runtime/state.enc.json"))
    state_decrypt_parser = state_commands.add_parser(
        "decrypt", help="Decrypt and validate one private state bundle"
    )
    state_decrypt_parser.add_argument("--input", type=Path, required=True)
    state_decrypt_parser.add_argument("--output-dir", type=Path, default=Path("runtime"))
    ranking_parser = subcommands.add_parser(
        "ranking", help="Manage local immutable ranking weight-set versions"
    )
    ranking_commands = ranking_parser.add_subparsers(dest="ranking_command", required=True)
    ranking_list_parser = ranking_commands.add_parser(
        "weights", help="List registered weight-set versions"
    )
    ranking_list_parser.add_argument("--state", type=Path)
    ranking_register_parser = ranking_commands.add_parser(
        "register-weights", help="Register one immutable locally evaluated weight set"
    )
    ranking_register_parser.add_argument("--state", type=Path)
    ranking_register_parser.add_argument("--version", required=True)
    ranking_register_parser.add_argument("--interest", required=True, type=float)
    ranking_register_parser.add_argument("--recency", required=True, type=float)
    ranking_register_parser.add_argument("--feedback", required=True, type=float)
    ranking_register_parser.add_argument("--identity", required=True, type=float)
    ranking_register_parser.add_argument("--scientific-quality", required=True, type=float)
    ranking_register_parser.add_argument("--reproducibility", required=True, type=float)
    ranking_register_parser.add_argument("--context", required=True, type=float)
    ranking_register_parser.add_argument("--negative-feedback-cap", required=True, type=float)
    ranking_activate_parser = ranking_commands.add_parser(
        "activate-weights", help="Point local ranking at a previously registered version"
    )
    ranking_activate_parser.add_argument("--state", type=Path)
    ranking_activate_parser.add_argument("--version", required=True)
    ranking_activate_parser.add_argument(
        "--shadow-report", type=Path, required=True, help="Eligible local report for this version"
    )
    arxiv_parser = subcommands.add_parser("arxiv", help="Retrieve public arXiv candidate metadata")
    arxiv_commands = arxiv_parser.add_subparsers(dest="arxiv_command", required=True)
    arxiv_retrieve_parser = arxiv_commands.add_parser(
        "retrieve", help="Retrieve profile categories"
    )
    arxiv_retrieve_parser.add_argument("--profile", type=Path, required=True)
    arxiv_retrieve_parser.add_argument(
        "--state", type=Path, default=Path("runtime/arxiv-state.json")
    )
    recommend_parser = subcommands.add_parser(
        "recommend", help="Generate validated recommendations"
    )
    recommend_commands = recommend_parser.add_subparsers(dest="recommend_command", required=True)
    recommend_run_parser = recommend_commands.add_parser(
        "run", help="Run the bounded recommendation pipeline"
    )
    recommend_run_parser.add_argument("--profile", type=Path, required=True)
    recommend_run_parser.add_argument("--candidate-state", type=Path, required=True)
    recommend_run_parser.add_argument(
        "--feedback-state", type=Path, default=Path("runtime/feedback-state.json")
    )
    recommend_run_parser.add_argument(
        "--cache", type=Path, default=Path("runtime/proposal-cache.json")
    )
    recommend_run_parser.add_argument(
        "--weight-state", type=Path, help="Local immutable ranking weight-set registry"
    )
    recommend_run_parser.add_argument(
        "--output", type=Path, default=Path("runtime/publishable-recommendations.json")
    )
    recommend_run_parser.add_argument(
        "--history", type=Path, default=Path("runtime/recommendation-history.json")
    )
    recommend_run_parser.add_argument(
        "--prepared-history", type=Path, default=Path("runtime/recommendation-history.next.json")
    )
    recommend_run_parser.add_argument(
        "--manifest", type=Path, default=Path("runtime/run-manifest.json")
    )
    recommend_run_parser.add_argument("--workflow-run-id", type=int)
    recommend_run_parser.add_argument("--workflow-run-attempt", type=int)
    recommend_run_parser.add_argument("--source-revision")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a supported command and return a stable automation-friendly exit code."""

    args = build_parser().parse_args(argv)
    try:
        config = load_config(
            config_path=args.config,
            overrides={"zotero_base_url": args.zotero_base_url},
        )
        if args.command == "schedule":
            print(
                generation_decision(
                    datetime.now(UTC),
                    event_name=args.event_name,
                    allow_peak_generation=args.allow_peak_generation,
                )
            )
            return 0
        if args.command == "doctor":
            diagnostics = run_doctor(config, check_zotero=not args.skip_zotero_check)
            _render_diagnostics(diagnostics, args.format)
            return int(doctor_exit_code(diagnostics))
        if args.command == "profile" and args.profile_command == "sync":
            database_path = args.database or Path(config.local_database_path)
            result = synchronize(
                ZoteroLocalClient(config.zotero_base_url), ZoteroStore(database_path)
            )
            if args.format == "json":
                print(json.dumps(asdict(result), ensure_ascii=False))
            else:
                print(
                    f"{result.mode} sync complete: {result.items_written} written, "
                    f"{result.items_unchanged} unchanged, {result.items_deleted} deleted"
                )
            return 0
        if args.command == "profile" and args.profile_command == "build":
            store = ZoteroStore(args.database or Path(config.local_database_path))
            remote, cache_hits = build_cached_remote_profile(
                store,
                args.payload_budget,
                watched_authors=tuple(
                    WatchedIdentity(item.name, item.aliases) for item in config.watched_authors
                ),
                watched_institutions=tuple(
                    WatchedIdentity(item.name, item.aliases) for item in config.watched_institutions
                ),
                curated_item_keys=local_curated_item_keys(args.corpus_state),
            )
            write_remote_profile(remote, args.output)
            print(
                "profile exported: "
                f"{len(remote.topics)} topics, {len(remote.core_categories)} categories, "
                f"{cache_hits} cache hits"
            )
            return 0
        if args.command == "profile" and args.profile_command == "publish-github":
            if not config.github_repository:
                raise ApplicationError("set ZAD_GITHUB_REPOSITORY before publishing a profile")
            publish_github_secret(
                read_remote_profile(args.input), config.github_repository, args.secret_name
            )
            print("protected profile published to GitHub Secret")
            return 0
        if args.command == "site" and args.site_command == "build":
            candidate_pool_status = ArxivStateStore(args.candidate_state).retrieval_status()
            site_result = build_site(
                read_published_set(args.input),
                args.output,
                public_output=config.public_output,
                passphrase=config.pages_passphrase,
                feedback_repository=config.github_repository,
                candidate_pool_status=candidate_pool_status,
            )
            mode = "public" if not site_result.encrypted else "encrypted"
            print(f"{mode} site built: {site_result.recommendation_count} recommendations")
            return 0
        if args.command == "feedback" and args.feedback_command == "ingest":
            feedback_result = FeedbackStateStore(args.state).ingest(read_github_issues(args.input))
            print(
                "feedback ingested: "
                f"{feedback_result.action_count} actions, "
                f"{feedback_result.duplicate_issues} duplicates"
            )
            return 0
        if args.command == "feedback" and args.feedback_command == "activate":
            activation = FeedbackLedgerStore(args.state).activate_weekly(
                datetime.now(UTC),
                interval_days=config.feedback_activation_interval_days,
                minimum_independent_papers=config.feedback_minimum_independent_papers,
            )
            print(f"feedback activation: {activation.decision}")
            return 0
        if args.command == "feedback" and args.feedback_command == "record-impressions":
            published = read_published_set(args.input)
            completed = published.generation_completed_at or published.generation_started_at
            occurred_at = datetime.fromisoformat(completed)
            batch_id = f"published-{published.generation_started_at}"
            added, duplicates = FeedbackLedgerStore(args.state).record_impressions(
                batch_id,
                tuple(record.arxiv_id for record in published.recommendations),
                occurred_at,
            )
            print(f"feedback impressions: {added} recorded, {duplicates} duplicates")
            return 0
        if args.command == "corpus" and args.corpus_command == "import-zotero":
            store = ZoteroStore(args.database or Path(config.local_database_path))
            mapping = CuratedCorpusMapping(
                tuple(args.positive_collection), tuple(args.negative_collection)
            )
            items = tuple(
                ZoteroCorpusItem(
                    source.item_key, source.identifiers, source.collections, source.tags
                )
                for source in store.corpus_sources()
            )
            corpus_result = CorpusStore(args.state).import_zotero(items, mapping, datetime.now(UTC))
            print(
                "curated corpus imported: "
                f"{corpus_result.added_events} events, "
                f"{corpus_result.unlabeled_events} unlabel corrections, "
                f"{corpus_result.skipped_items} skipped, revision {corpus_result.revision}"
            )
            return 0
        if args.command == "corpus" and args.corpus_command == "list-collections":
            store = ZoteroStore(args.database or Path(config.local_database_path))
            for collection in store.collections():
                print(f"{collection.key}\t{collection.name}")
            return 0
        if args.command == "evidence" and args.evidence_command == "enrich":
            evidence = OpenAlexEvidenceEnricher(OpenAlexClient(), EvidenceCache(args.cache)).enrich(
                ArxivStateStore(args.candidate_state).candidates(),
                datetime.now(UTC),
                limit=args.limit,
            )
            available = sum(
                item.context is not None
                and item.context.open_access.availability.value == "available"
                for item in evidence
            )
            print(
                f"public evidence enriched: {len(evidence)} candidates, {available} context records"
            )
            return 0
        if args.command == "state":
            if not config.state_encryption_key:
                raise ApplicationError(
                    "set ZAD_STATE_ENCRYPTION_KEY before handling workflow state"
                )
            if args.state_command == "encrypt":
                encrypt_state_directory(args.input_dir, args.output, config.state_encryption_key)
                print(f"encrypted workflow state: {args.output}")
                return 0
            if args.state_command == "decrypt":
                files = decrypt_state_bundle(
                    args.input, args.output_dir, config.state_encryption_key
                )
                print(f"decrypted workflow state: {len(files)} files")
                return 0
        if args.command == "evaluate" and args.evaluate_command == "record-manifest":
            count = record_manifest(args.input, args.history)
            print(f"run manifest recorded: {count} entries")
            return 0
        if args.command == "evaluate" and args.evaluate_command == "efficiency":
            comparison = compare_manifest_files(args.baseline, args.candidate, args.output)
            state = "measured" if comparison.comparable else "observation-only"
            print(f"efficiency observation written: {state}")
            return 0
        if args.command == "evaluate" and args.evaluate_command == "snapshot":
            created_at = datetime.now(UTC)
            corpus = CorpusStore(args.corpus_state).snapshot(created_at)
            snapshot = make_evaluation_snapshot(
                corpus,
                created_at=created_at,
                anchor_paper_ids=tuple(args.anchor_paper_id),
                rolling_days=args.rolling_days,
            )
            EvaluationSnapshotStore(args.snapshots).write(snapshot)
            print(
                "evaluation snapshot written: "
                f"{snapshot.snapshot_id}, {snapshot.label_count} labels"
            )
            return 0
        if args.command == "evaluate" and args.evaluate_command == "shadow":
            snapshot = EvaluationSnapshotStore(args.snapshots).read(args.snapshot_id)
            weight_registry = WeightSetRegistry(
                args.weight_state or Path(config.ranking_weight_state_path)
            )
            weight_registry.register(DEFAULT_WEIGHT_SET)
            report = run_shadow_evaluation(
                ArxivStateStore(args.candidate_state).candidates(),
                read_remote_profile(args.profile),
                snapshot,
                snapshot.cutoff_at,
                feedback_adjustments=FeedbackStateStore(args.feedback_state).adjustments(),
                weight_set=weight_registry.active(),
            )
            write_shadow_report(report, args.output)
            gate_state = (
                "eligible-for-manual-review"
                if report.eligible_for_activation and report.warnings
                else "eligible"
                if report.eligible_for_activation
                else "provisional-or-blocked"
            )
            print(f"shadow evaluation written: {gate_state}, {len(report.reasons)} gate reason(s)")
            return 0
        if args.command == "evaluate" and args.evaluate_command == "hydrate-candidates":
            snapshot = EvaluationSnapshotStore(args.snapshots).read(args.snapshot_id)
            hydrated = hydrate_labeled_candidates(
                ArxivClient(),
                tuple(paper_id for paper_id, _ in snapshot.labels),
                batch_size=args.batch_size,
            )
            if hydrated.candidates:
                ArxivStateStore(args.candidate_state).commit(
                    RetrievalCheckpoint(datetime.now(UTC)),
                    hydrated.candidates,
                    retention_days=None,
                )
            print(
                "evaluation candidates hydrated: "
                f"{len(hydrated.candidates)} exact matches, "
                f"{len(hydrated.unresolved_ids)} unresolved, "
                f"{hydrated.request_count} public requests"
            )
            return 0
        if args.command == "ranking" and args.ranking_command == "weights":
            registry = WeightSetRegistry(args.state or Path(config.ranking_weight_state_path))
            registry.register(DEFAULT_WEIGHT_SET)
            versions = registry.versions()
            active = registry.active().version
            for weight_set in versions:
                marker = "*" if weight_set.version == active else " "
                print(f"{marker} {weight_set.version}")
            return 0
        if args.command == "ranking" and args.ranking_command == "register-weights":
            registry = WeightSetRegistry(args.state or Path(config.ranking_weight_state_path))
            weight_set = WeightSet(
                args.version,
                args.interest,
                args.recency,
                args.feedback,
                args.identity,
                args.scientific_quality,
                args.reproducibility,
                args.context,
                args.negative_feedback_cap,
            )
            registered = registry.register(weight_set)
            action = "registered" if registered else "already registered"
            print(f"ranking weight set {action}: {weight_set.version}")
            return 0
        if args.command == "ranking" and args.ranking_command == "activate-weights":
            registry = WeightSetRegistry(args.state or Path(config.ranking_weight_state_path))
            try:
                report = json.loads(args.shadow_report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ApplicationError("shadow report is unreadable") from error
            if (
                not isinstance(report, dict)
                or report.get("weight_set_version") != args.version
                or report.get("eligible_for_activation") is not True
            ):
                raise ApplicationError("shadow report does not approve this ranking weight set")
            active_weight = registry.activate(args.version)
            print(f"ranking weight set activated: {active_weight.version}")
            return 0
        if args.command == "arxiv" and args.arxiv_command == "retrieve":
            profile = read_remote_profile(args.profile)
            categories = profile.core_categories + expand_one_hop(profile.core_categories)
            retrieval = retrieve(
                ArxivClient(), ArxivStateStore(args.state), categories, datetime.now(UTC)
            )
            print(f"arXiv retrieval complete: {len(retrieval.candidates)} candidates")
            return 0
        if args.command == "recommend" and args.recommend_command == "run":
            if not config.deepseek_api_key:
                raise ApplicationError("set ZAD_DEEPSEEK_API_KEY before generating recommendations")
            profile = read_remote_profile(args.profile)
            feedback = FeedbackStateStore(args.feedback_state)
            started_at = datetime.now(UTC)
            history = RecommendationHistoryStore(args.history)
            weight_registry = WeightSetRegistry(
                args.weight_state or Path(config.ranking_weight_state_path)
            )
            weight_registry.register(DEFAULT_WEIGHT_SET)
            weight_set = weight_registry.active()
            candidate_store = ArxivStateStore(args.candidate_state)
            candidates = candidate_store.candidates()
            provider = DeepSeekClient(
                config.deepseek_api_key,
                timeout_seconds=config.deepseek_timeout_seconds,
                output_language=config.output_language,
                max_output_tokens=config.llm_max_output_tokens,
                proposal_prompt_version="proposal-v2",
            )
            feedback_adjustments = feedback.adjustments()
            excluded_ids = history.excluded_ids(started_at, config.recommendation_suppression_days)
            if config.llm_refinement_enabled:
                recommendation_set, manifest = run_refined_recommendation(
                    candidates,
                    profile,
                    started_at,
                    provider,
                    ProposalCache(args.cache),
                    model="deepseek-v4-flash",
                    output_language=config.output_language,
                    allow_preference_context=config.llm_preference_context_approved,
                    feedback_adjustments=feedback_adjustments,
                    pre_rank_limit=config.recommendation_candidate_limit,
                    excluded_ids=excluded_ids,
                    author_bonus=config.author_preference_bonus,
                    institution_bonus=config.institution_preference_bonus,
                    identity_bonus_cap=config.identity_bonus_cap,
                    weight_set=weight_set,
                    judge_batch_size=config.llm_judge_batch_size,
                    explanation_batch_size=config.llm_explanation_batch_size,
                    max_request_tokens=config.llm_request_token_limit,
                    max_request_bytes=config.llm_request_byte_limit,
                    max_requests=config.llm_max_requests,
                    retries=config.llm_retries,
                )
            else:
                recommendation_set, manifest = run_recommendation(
                    candidates,
                    profile,
                    started_at,
                    provider,
                    ProposalCache(args.cache),
                    prompt_version=f"recommendation-v3:proposal-v2:{config.output_language.casefold()}",
                    model="deepseek-v4-flash",
                    feedback_adjustments=feedback_adjustments,
                    pre_rank_limit=config.recommendation_candidate_limit,
                    excluded_ids=excluded_ids,
                    author_bonus=config.author_preference_bonus,
                    institution_bonus=config.institution_preference_bonus,
                    identity_bonus_cap=config.identity_bonus_cap,
                    weight_set=weight_set,
                    batch_size=config.llm_judge_batch_size,
                    max_requests=config.llm_max_requests,
                    max_request_tokens=config.llm_request_token_limit,
                    max_request_bytes=config.llm_request_byte_limit,
                    retries=config.llm_retries,
                )
            degraded, degraded_reason, source_checkpoint = candidate_store.retrieval_status()
            manifest = replace(
                manifest,
                candidate_pool_degraded=degraded,
                candidate_pool_degraded_reason=degraded_reason,
                candidate_pool_source_checkpoint=source_checkpoint,
            )
            workflow_run = _workflow_run(args, config.github_repository)
            write_published_set(
                make_published_set(
                    recommendation_set,
                    profile_schema_version=profile.schema_version,
                    workflow_run=workflow_run,
                    output_language=config.output_language,
                ),
                args.output,
            )
            history.prepare_success(
                recommendation_set,
                args.prepared_history,
                recommendation_set.generation_completed_at or started_at,
            )
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(
                json.dumps(
                    asdict(manifest), ensure_ascii=False, default=str, separators=(",", ":")
                ),
                encoding="utf-8",
            )
            print(
                "recommendations generated: "
                f"{manifest.recommendation_count} selected, "
                f"{manifest.model_requests} model requests"
            )
            return 0
    except ApplicationError as error:
        prefix = "configuration error" if error.exit_code == 2 else "operation error"
        print(f"{prefix}: {error}")
        return int(error.exit_code)
    raise AssertionError(f"unsupported command: {args.command}")


def _workflow_run(args: argparse.Namespace, repository: str | None) -> WorkflowRun | None:
    values = (args.workflow_run_id, args.workflow_run_attempt, args.source_revision, repository)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ApplicationError("workflow run metadata must be supplied as a complete set")
    run_id = int(args.workflow_run_id)
    return WorkflowRun(
        run_id,
        int(args.workflow_run_attempt),
        str(args.source_revision),
        str(repository),
        f"https://github.com/{repository}/actions/runs/{run_id}",
    )


def _render_diagnostics(diagnostics: Sequence[Diagnostic], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps([item.to_dict() for item in diagnostics], ensure_ascii=False))
        return
    for item in diagnostics:
        print(f"{item.name}: {item.state.value} — {item.detail}")


if __name__ == "__main__":
    raise SystemExit(main())
