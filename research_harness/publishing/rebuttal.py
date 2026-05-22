from __future__ import annotations

from pathlib import Path
from typing import Any


def build_rebuttal_packet(state: dict[str, Any], output_path: Path) -> str:
    node = state["node"]
    report = state["worker_report"]
    reduction = state["orchestrator_reduction"]

    lines = [
        "# Rebuttal Packet",
        "",
        "## Target Claims",
        f"- {node['claim_contract']['claim_under_test']}",
        "",
        "## Scope Boundaries",
        "- This v0 packet demonstrates the harness architecture, not a live Claude Code experiment.",
        "- The mock worker is used only for orchestration and schema smoke testing.",
        "",
        "## Evidence Summary",
        f"- Worker status: {report['status']}",
        f"- Worker verdict candidate: {report['claim_verdict_candidate']}",
        f"- Final verdict: {reduction['final_verdict']}",
        f"- Research status: {reduction['research_status']}",
        "",
        "## Baselines",
    ]
    for ref in node["baseline_refs"]:
        roles = ", ".join(ref["roles"])
        lines.append(f"- {ref['baseline_dossier_id']}: {roles}")

    lines.extend(
        [
            "",
            "## Critic History",
        ]
    )
    if reduction["blocking_objections"]:
        for objection in reduction["blocking_objections"]:
            lines.append(f"- Blocker: {objection['objection']}")
    else:
        lines.append("- No blocking objections in the node-level mock review.")

    lines.extend(
        [
            "",
            "## Failure and Lessons",
        ]
    )
    if reduction["accepted_lesson_candidates"]:
        for lesson in reduction["accepted_lesson_candidates"]:
            lines.append(f"- Lesson candidate: {lesson}")
    else:
        lines.append("- No lesson candidates accepted.")

    lines.extend(
        [
            "",
            "## Publication Plan",
            "- Preferred outputs are controlled by settings.json.",
            "- TeX is disabled by default to avoid token-heavy drafting until explicitly needed.",
            "",
            "## Artifact Index",
        ]
    )
    for artifact in report["artifacts"]:
        lines.append(f"- {artifact}")

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")
    return text


def build_orchestrator_rebuttal(state: dict[str, Any], output_path: Path) -> str:
    reduction = state["orchestrator_reduction"]
    lines = [
        "# Orchestrator Rebuttal",
        "",
        "This v0 mock run does not execute additional experiments.",
        "A real rebuttal session may run only limited-depth, objection-linked checks.",
        "",
        "## Response",
    ]
    if reduction["blocking_objections"]:
        for objection in reduction["blocking_objections"]:
            lines.append(f"- Needs resolution: {objection['required_resolution']}")
    else:
        lines.append("- No publication-blocking node-level objection was raised in the mock run.")

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")
    return text

