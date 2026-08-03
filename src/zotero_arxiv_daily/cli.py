"""Thin command-line entry point for currently implemented use cases."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily import __version__
from zotero_arxiv_daily.arxiv.categories import expand_one_hop
from zotero_arxiv_daily.arxiv.client import ArxivClient
from zotero_arxiv_daily.arxiv.retrieval import retrieve
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.config import load_config
from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.core.time import generation_decision
from zotero_arxiv_daily.doctor import Diagnostic, doctor_exit_code, run_doctor
from zotero_arxiv_daily.evaluation.corpus import (
    CorpusStore,
    CuratedCorpusMapping,
    ZoteroCorpusItem,
)
from zotero_arxiv_daily.evidence.openalex import OpenAlexClient, OpenAlexEvidenceEnricher
from zotero_arxiv_daily.evidence.storage import EvidenceCache
from zotero_arxiv_daily.feedback.ingest import FeedbackStateStore, read_github_issues
from zotero_arxiv_daily.feedback.ledger import FeedbackLedgerStore
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.deepseek import DeepSeekClient
from zotero_arxiv_daily.pipeline.recommend import run_recommendation
from zotero_arxiv_daily.profile.export import write_remote_profile
from zotero_arxiv_daily.profile.models import WatchedIdentity
from zotero_arxiv_daily.profile.service import (
    build_cached_remote_profile,
    local_curated_item_keys,
    publish_github_secret,
    read_remote_profile,
)
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
            site_result = build_site(
                read_published_set(args.input),
                args.output,
                public_output=config.public_output,
                passphrase=config.pages_passphrase,
                feedback_repository=config.github_repository,
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
            recommendation_set, manifest = run_recommendation(
                ArxivStateStore(args.candidate_state).candidates(),
                profile,
                started_at,
                DeepSeekClient(
                    config.deepseek_api_key,
                    timeout_seconds=config.deepseek_timeout_seconds,
                    output_language=config.output_language,
                ),
                ProposalCache(args.cache),
                prompt_version=f"recommendation-v2:{config.output_language.casefold()}",
                model="deepseek-v4-flash",
                feedback_adjustments=feedback.adjustments(),
                pre_rank_limit=config.recommendation_candidate_limit,
                excluded_ids=history.excluded_ids(
                    started_at, config.recommendation_suppression_days
                ),
                author_bonus=config.author_preference_bonus,
                institution_bonus=config.institution_preference_bonus,
                identity_bonus_cap=config.identity_bonus_cap,
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
