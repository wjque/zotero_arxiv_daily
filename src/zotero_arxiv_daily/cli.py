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
from zotero_arxiv_daily.doctor import Diagnostic, doctor_exit_code, run_doctor
from zotero_arxiv_daily.feedback.ingest import FeedbackStateStore, read_github_issues
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.deepseek import DeepSeekClient
from zotero_arxiv_daily.pipeline.recommend import run_recommendation
from zotero_arxiv_daily.profile.export import write_remote_profile
from zotero_arxiv_daily.profile.service import (
    build_cached_remote_profile,
    publish_github_secret,
    read_remote_profile,
)
from zotero_arxiv_daily.site.build import build_site
from zotero_arxiv_daily.site.models import (
    make_published_set,
    read_published_set,
    write_published_set,
)
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a supported command and return a stable automation-friendly exit code."""

    args = build_parser().parse_args(argv)
    try:
        config = load_config(
            config_path=args.config,
            overrides={"zotero_base_url": args.zotero_base_url},
        )
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
            remote, cache_hits = build_cached_remote_profile(store, args.payload_budget)
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
            recommendation_set, manifest = run_recommendation(
                ArxivStateStore(args.candidate_state).candidates(),
                profile,
                datetime.now(UTC),
                DeepSeekClient(
                    config.deepseek_api_key,
                    timeout_seconds=config.deepseek_timeout_seconds,
                    output_language=config.output_language,
                ),
                ProposalCache(args.cache),
                prompt_version="recommendation-v1",
                model="deepseek-v4-flash",
                feedback_adjustments=feedback.adjustments(),
                pre_rank_limit=config.recommendation_candidate_limit,
            )
            write_published_set(make_published_set(recommendation_set), args.output)
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


def _render_diagnostics(diagnostics: Sequence[Diagnostic], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps([item.to_dict() for item in diagnostics], ensure_ascii=False))
        return
    for item in diagnostics:
        print(f"{item.name}: {item.state.value} — {item.detail}")


if __name__ == "__main__":
    raise SystemExit(main())
