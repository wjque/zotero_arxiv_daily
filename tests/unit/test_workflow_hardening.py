from __future__ import annotations

from pathlib import Path

from zotero_arxiv_daily.security.state import ALLOWED_STATE_FILES, OPTIONAL_STATE_FILES
from zotero_arxiv_daily.site.models import PublishedRecommendationSet, published_batch_id


def test_daily_workflow_guards_model_cost_and_promotes_history_after_deployment() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert 'cron: "30 10 * * *"' in workflow
    assert "allow_peak_generation" in workflow
    assert "enable_llm_refinement" in workflow
    assert "use_v012_ranking" in workflow
    assert "approve_llm_preference_context" in workflow
    assert "purge_legacy_state_history" in workflow
    assert "capture_efficiency_baseline" in workflow
    assert "compare_efficiency_candidate" in workflow
    assert "simulate_post_deploy_state_push_failure" in workflow
    assert (
        "ZAD_LLM_REFINEMENT_ENABLED: ${{ (github.event_name == 'schedule' || "
        "inputs.enable_llm_refinement) && 'true' || 'false' }}" in workflow
    )
    assert (
        "ZAD_LLM_PREFERENCE_CONTEXT_APPROVED: "
        "${{ inputs.approve_llm_preference_context && 'true' || 'false' }}" in workflow
    )
    assert "generation-window.outputs.decision == 'allowed'" in workflow
    assert workflow.index("Deploy GitHub Pages") < workflow.index("Persist successful run state")
    assert "git worktree add -B state runtime/state origin/state" in workflow
    assert "ZAD_STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}" in workflow
    assert "ZAD_PROFILE_FEATURE_KEY: ${{ secrets.ZAD_PROFILE_FEATURE_KEY }}" in workflow
    assert "printf '%s' \"$REMOTE_PROFILE\" > runtime/remote-profile.json" in workflow
    assert "state decrypt" in workflow
    assert "state encrypt" in workflow
    assert "Purge legacy plaintext state history" in workflow
    assert "push --force-with-lease=refs/heads/state" in workflow
    assert "State tip still contains plaintext protected state" in workflow
    assert "cp runtime/state.enc.json runtime/state/" in workflow
    assert "git -C runtime/state rm -f -- '*.json'" in workflow
    assert "run-manifest-history.json" in workflow
    assert "cp runtime/arxiv-state.json runtime/feedback-state.json" not in workflow
    assert "evaluate record-manifest" in workflow
    assert "Capture or compare privacy-safe efficiency evidence" in workflow
    assert "Privacy-safe efficiency comparison" in workflow
    assert "quality is the active canary criterion" in workflow
    assert "ranking_arguments+=(--ranking-mode v0.1.2)" in workflow
    assert "Simulating the post-deploy state-push failure" in workflow
    assert "timeout: 1200000" not in workflow
    assert workflow.count("timeout: 600000") == 2
    assert "continue-on-error: true" in workflow
    assert "Wait before retrying GitHub Pages deployment" in workflow
    assert "run: sleep 60" in workflow
    assert "Retry GitHub Pages deployment" in workflow
    assert "steps.deployment.outcome == 'failure'" in workflow
    assert "steps.deployment-retry.outcome == 'success'" in workflow
    assert (
        "steps.deployment.outputs.page_url || steps.deployment-retry.outputs.page_url" in workflow
    )
    assert (
        workflow.count(
            "cp runtime/recommendation-history.next.json runtime/recommendation-history.json"
        )
        == 1
    )
    promotion = workflow.index(
        "cp runtime/recommendation-history.next.json runtime/recommendation-history.json"
    )
    assert workflow.index("Persist successful run state") < promotion
    assert (
        "steps.deployment-retry.outcome == 'success'"
        in workflow[workflow.index("Persist successful run state") : promotion]
    )
    assert 'receipt["deployed_at"] = datetime.now(UTC).isoformat()' in workflow


def test_daily_workflow_reconciles_a_deployed_batch_after_state_push_failure() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert "id: restore-state" in workflow
    assert 'echo "reconcile=true" >> "$GITHUB_OUTPUT"' in workflow
    assert "pending-publishable-recommendations.json" in workflow
    assert 'gh run view "$receipt_run_id" --attempt "$receipt_attempt" --json jobs' in workflow
    assert 'deployment_steps = {"Deploy GitHub Pages", "Retry GitHub Pages deployment"}' in workflow
    assert "Reconcile previously deployed batch" in workflow
    assert (
        "feedback record-impressions --input runtime/pending-publishable-recommendations.json"
        in workflow
    )
    assert '"status"] = "reconciled"' in workflow
    assert (
        "cp runtime/pending-recommendation-history.json runtime/recommendation-history.json"
        in workflow
    )
    assert (
        "cp runtime/pending-recommendation-history.json runtime/recommendation-history.next.json"
        not in workflow
    )
    assert "git -C runtime/state push origin HEAD:state" in workflow


def test_metadata_validation_branch_cannot_publish_or_call_a_model() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
    validation = workflow.split("- name: Refresh public metadata without publication", 1)[1].split(
        "- name: Retrieve feedback and generate static site", 1
    )[0]

    assert "arxiv retrieve" in validation
    assert "controlled-shadow" not in workflow
    assert "validation record" in validation
    assert "state encrypt" in validation
    for forbidden in (
        "DEEPSEEK_API_KEY",
        "recommend run",
        "site build",
        "upload-pages-artifact",
        "deploy-pages",
        "record-impressions",
        "recommendation-history",
        "pending-publishable",
    ):
        assert forbidden not in validation


def test_workflows_pin_node24_compatible_action_revisions() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(".github/workflows").glob("*.yml")
    )

    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803" in workflows
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in workflows
    assert "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d" in workflows
    assert "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9" in workflows
    assert "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128" in workflows


def test_quality_profile_workflow_is_protected_private_and_non_publishing() -> None:
    workflow = Path(".github/workflows/quality-profile.yml").read_text(encoding="utf-8")

    assert "environment: production" in workflow
    assert "group: zotero-arxiv-daily-production" in workflow
    assert "contents: write" in workflow
    assert "issues: read" not in workflow
    assert "pages: write" not in workflow
    assert "id-token: write" not in workflow
    assert "secrets.STATE_ENCRYPTION_KEY" in workflow
    assert "secrets.QUALITY_PROFILE_EXAMPLES" in workflow
    assert "git worktree add --detach runtime/state FETCH_HEAD" in workflow
    assert "state decrypt" in workflow
    assert "state encrypt" in workflow
    assert "quality-profile generate" in workflow
    assert '--policy-version "$POLICY_VERSION"' in workflow
    assert "quality-profile inspect" in workflow
    assert 'quality-profile "$OPERATION" --version "$PROFILE_VERSION"' in workflow
    assert "quality-profile clear-approval" in workflow
    assert "inputs.operation != 'inspect'" in workflow
    assert "cmp -s runtime/quality-profile.before.json runtime/quality-profile.json" in workflow
    assert "git -C runtime/state push origin HEAD:state" in workflow
    assert "push --force" not in workflow
    for forbidden in (
        "DEEPSEEK_API_KEY",
        "recommend run",
        "site build",
        "upload-pages-artifact",
        "deploy-pages",
        "record-impressions",
        "gh issue list",
    ):
        assert forbidden not in workflow


def test_daily_workflow_reports_scientific_value_filter_count() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert '"scientific_value_filtered_count"' in workflow


def test_daily_workflow_guards_every_allowlisted_state_file_as_plaintext() -> None:
    """A state file the bundle may carry must also be refused in plaintext on the state branch."""

    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
    guard = next(line for line in workflow.splitlines() if "for plaintext_state in" in line)
    restore = next(line for line in workflow.splitlines() if "for optional in" in line)

    for name in ALLOWED_STATE_FILES:
        assert name in guard, name
    for name in OPTIONAL_STATE_FILES:
        assert name in restore, name


def test_daily_workflow_batch_id_mirrors_the_python_definition() -> None:
    """The receipt and the browser build their own copy of the impression batch ID format."""

    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
    site = Path("src/zotero_arxiv_daily/site/build.py").read_text(encoding="utf-8")
    published = PublishedRecommendationSet(5, "2026-08-02T00:00:00+00:00", ())

    assert published_batch_id(published) == "published-2026-08-02T00:00:00+00:00"
    assert '"batch_id": f"published-{started}"' in workflow
    assert "`published-${state.data.generation_started_at||state.data.generated_at}`" in site
