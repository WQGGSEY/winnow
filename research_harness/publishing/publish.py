from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from research_harness.publishing.html import render_interactive_html
from research_harness.publishing.paper_html import render_paper_html
from research_harness.publishing.slides_html import render_slides_html


class PublishError(ValueError):
    """Raised when publish dispatch input is invalid."""


Renderer = Callable[[dict[str, Any], Path], None]


def _render_markdown_paper(state: dict[str, Any], output_path: Path) -> None:
    node = state["node"]
    reduction = state["orchestrator_reduction"]
    ac_decision = state["ac_decision"]
    worker_report = state["worker_report"]
    lines = [
        "# " + node["claim_contract"]["claim_under_test"],
        "",
        f"- Node: {node['id']} | Domain: {node.get('domain', 'n/a')} | Stage: {node.get('stage', 'n/a')}",
        f"- Research status: {reduction['research_status']}",
        f"- AC decision: {ac_decision['decision']} (confidence {ac_decision.get('confidence', 'n/a')})",
        "",
        "## Mandatory Baselines",
    ]
    for baseline in node["claim_contract"]["mandatory_baselines"]:
        lines.append(f"- {baseline}")
    lines.extend(["", "## Success Criteria"])
    for criterion in node["claim_contract"]["success_criteria"]:
        lines.append(f"- {criterion}")
    lines.extend(["", "## Disproof Conditions"])
    for condition in node["claim_contract"]["disproof_conditions"]:
        lines.append(f"- {condition}")
    lines.extend(
        [
            "",
            "## Evidence",
            f"- Worker status: {worker_report['status']}",
            f"- Worker verdict candidate: {worker_report['claim_verdict_candidate']}",
            f"- Orchestrator final verdict: {reduction['final_verdict']}",
            "",
            "## AC Scores",
        ]
    )
    for key, value in ac_decision["score_summary"].items():
        lines.append(f"- {key}: {value}")
    if ac_decision.get("camera_ready_conditions"):
        lines.extend(["", "## Camera-Ready Conditions"])
        for condition in ac_decision["camera_ready_conditions"]:
            lines.append(f"- {condition}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


RENDERERS: dict[str, tuple[Renderer, str]] = {
    "interactive_html": (render_interactive_html, "interactive_summary.html"),
    "slides_html": (render_slides_html, "slides_summary.html"),
    "markdown_paper": (_render_markdown_paper, "paper.md"),
    "paper_html": (render_paper_html, "paper.html"),
}


def publish_state_bundle(
    state: dict[str, Any],
    settings: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Render configured publication artifacts only when AC accepted the bundle.

    The dispatcher is intentionally narrow: it does not modify state, does not
    re-run critics, and does not emit artifacts for revise/reject, and refuses
    to render anything when ``state["evidence_is_fake"]`` is true (which the
    production runner sets when any promoted node ran on the demo fallback
    plan instead of a real domain template). It writes a
    publication_dispatch.json next to the artifacts so operators can audit
    which renderers ran and why.
    """

    if "ac_decision" not in state:
        raise PublishError("state bundle is missing ac_decision; nothing to publish")
    ac_decision = state["ac_decision"]
    decision = ac_decision.get("decision")
    evidence_is_fake = bool(state.get("evidence_is_fake", False))
    publishing = settings.get("publishing", {})
    requested = list(publishing.get("default_outputs") or [])
    allow_tex = bool(publishing.get("allow_tex", False))

    if "final_tex" in requested and not allow_tex:
        requested = [item for item in requested if item != "final_tex"]

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    dispatch_path = output_dir / "publication_dispatch.json"
    base = {
        "type": "publication_dispatch",
        "decision": decision,
        "evidence_is_fake": evidence_is_fake,
        "requested_outputs": requested,
        "allow_tex": allow_tex,
        "rendered_artifacts": [],
        "skipped_outputs": [],
        "dispatch_path": str(dispatch_path),
        "blocked_reason": None,
    }

    if evidence_is_fake:
        base["blocked_reason"] = (
            "evidence_is_fake=true; publish dispatcher refuses to render "
            "fallback-demo metrics. Add a domain template under "
            "experiment_plan_templates/<node.domain>.py and rerun."
        )
        _write_dispatch(dispatch_path, base)
        return base

    if decision != "accept":
        base["blocked_reason"] = (
            f"ac_decision is {decision!r}; renderers only run on accept"
        )
        _write_dispatch(dispatch_path, base)
        return base

    rendered: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for name in requested:
        spec = RENDERERS.get(name)
        if spec is None:
            skipped.append({"output": name, "reason": "unknown renderer"})
            continue
        renderer, filename = spec
        artifact_path = output_dir / filename
        renderer(state, artifact_path)
        rendered.append({"output": name, "artifact_path": str(artifact_path)})

    base["rendered_artifacts"] = rendered
    base["skipped_outputs"] = skipped
    _write_dispatch(dispatch_path, base)
    return base


def _write_dispatch(path: Path, dispatch: dict[str, Any]) -> None:
    path.write_text(json.dumps(dispatch, indent=2, sort_keys=True) + "\n", encoding="utf-8")
