from __future__ import annotations

from pathlib import Path


def test_daily_workflow_guards_model_cost_and_promotes_history_after_deployment() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert 'cron: "30 10 * * *"' in workflow
    assert "allow_peak_generation" in workflow
    assert "generation-window.outputs.decision == 'allowed'" in workflow
    assert workflow.index("Deploy GitHub Pages") < workflow.index("Persist successful run state")
    assert "recommendation-history.next.json runtime/state/recommendation-history.json" in workflow


def test_workflows_pin_node24_compatible_action_revisions() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(".github/workflows").glob("*.yml")
    )

    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803" in workflows
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in workflows
    assert "actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d" in workflows
    assert "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9" in workflows
    assert "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128" in workflows
