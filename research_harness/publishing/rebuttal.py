from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_rebuttal_packet(state: dict[str, Any], output_path: Path) -> str:
    node = state["node"]
    report = state["worker_report"]
    reduction = state["orchestrator_reduction"]

    metrics = report.get("metrics") or {}
    baselines = report.get("baselines") or {}
    baseline_status = report.get("baseline_evidence_status") or {}
    artifacts = report.get("artifacts") or []
    unexpected = report.get("unexpected_observations") or []
    disproof_hits = report.get("disproof_conditions_hit") or []
    critic_reviews = state.get("critic_reviews") or []
    score_summary = reduction.get("score_summary") or {}

    lines = [
        "# Rebuttal Packet",
        "",
        f"- Node: {node['id']} | type: {node['type']} | stage: {node['stage']} | domain: {node.get('domain', 'n/a')}",
        "",
        "## Target Claim",
        f"- {node['claim_contract']['claim_under_test']}",
        "",
        "## Evidence Summary",
        f"- Worker status: {report['status']}",
        f"- Worker verdict candidate: {report['claim_verdict_candidate']}",
        f"- Orchestrator final verdict: {reduction['final_verdict']}",
        f"- Research status: {reduction['research_status']}",
        f"- Next transition: {reduction['next_transition']}",
        f"- Critic reviews collected: {len(critic_reviews)}",
        f"- Metric keys reported: {', '.join(sorted(metrics.keys())) if metrics else '(none)'}",
        f"- Baseline keys reported: {', '.join(sorted(baselines.keys())) if baselines else '(none)'}",
        f"- Evidence artifacts: {len(artifacts)}",
        "",
        "## Critic Score Summary",
    ]
    if score_summary:
        for key in sorted(score_summary.keys()):
            lines.append(f"- {key}: {score_summary[key]}")
    else:
        lines.append("- (no scores recorded)")

    lines.extend(["", "## Baselines Declared"])
    if node.get("baseline_refs"):
        for ref in node["baseline_refs"]:
            roles = ", ".join(ref.get("roles", []))
            lines.append(f"- {ref['baseline_dossier_id']}: roles=[{roles}]")
    else:
        lines.append("- (none declared on node)")

    lines.extend(["", "## Baseline Evidence Status"])
    if baseline_status:
        overall = baseline_status.get("overall", "unknown")
        lines.append(f"- Overall: {overall}")
        for result in baseline_status.get("results", []) or []:
            status = result.get("status", "unknown")
            reason = result.get("reason") or ""
            role = result.get("role") or result.get("baseline_key") or "n/a"
            line = f"- {role}: {status}"
            if reason:
                line += f" — {reason}"
            lines.append(line)
    else:
        lines.append("- (no baseline evidence status reported)")

    lines.extend(["", "## Disproof Conditions Hit"])
    if disproof_hits:
        for condition in disproof_hits:
            lines.append(f"- {condition}")
    else:
        lines.append("- (none triggered)")

    lines.extend(["", "## Blocking Objections"])
    if reduction.get("blocking_objections"):
        for objection in reduction["blocking_objections"]:
            lines.append(f"- {objection.get('objection', '(no text)')}")
            required = objection.get("required_resolution")
            if required:
                lines.append(f"  - Required resolution: {required}")
    else:
        lines.append("- (none recorded by critic governance)")

    lines.extend(["", "## Unexpected Observations"])
    if unexpected:
        for obs in unexpected:
            text = obs.get("observation") or obs.get("evidence") or json.dumps(obs)
            scope_relation = obs.get("scope_relation")
            line = f"- {text}"
            if scope_relation:
                line += f" (scope: {scope_relation})"
            lines.append(line)
    else:
        lines.append("- (none reported by worker)")

    lines.extend(["", "## Lessons Accepted At Reduction"])
    if reduction.get("accepted_lesson_candidates"):
        for lesson in reduction["accepted_lesson_candidates"]:
            lines.append(f"- {lesson}")
    else:
        lines.append("- (no lesson candidates accepted)")

    lines.extend(
        [
            "",
            "## Publication Routing",
            "- AC decision determines whether publication renders.",
            "- The publish dispatcher (`research_harness.publishing.publish`) selects "
            "renderers from `settings.publishing.default_outputs` only when AC accepts.",
        ]
    )

    lines.extend(["", "## Artifact Index"])
    if artifacts:
        for artifact in artifacts:
            lines.append(f"- {artifact}")
    else:
        lines.append("- (none recorded by the worker report)")

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")
    return text


def build_orchestrator_rebuttal(state: dict[str, Any], output_path: Path) -> str:
    reduction = state["orchestrator_reduction"]
    report = state["worker_report"]
    blocking = reduction.get("blocking_objections") or []
    child_suggestions = reduction.get("child_branch_suggestions") or []
    accepted_lessons = reduction.get("accepted_lesson_candidates") or []

    lines = [
        "# Orchestrator Rebuttal",
        "",
        f"- Worker status: {report['status']}",
        f"- Worker verdict candidate: {report['claim_verdict_candidate']}",
        f"- Final verdict: {reduction['final_verdict']}",
        f"- Research status: {reduction['research_status']}",
        f"- Next transition: {reduction['next_transition']}",
        "",
        "## Response To Blocking Objections",
    ]
    if blocking:
        for objection in blocking:
            lines.append(f"- Objection: {objection.get('objection', '(no text)')}")
            required = objection.get("required_resolution")
            if required:
                lines.append(f"  - Required resolution: {required}")
    else:
        lines.append("- No publication-blocking objection was raised at the node-level reduction stage.")

    lines.extend(["", "## Limited-Depth Follow-Ups Proposed"])
    if child_suggestions:
        for suggestion in child_suggestions:
            stype = suggestion.get("type", "n/a")
            reason = suggestion.get("reason", "")
            source = suggestion.get("source", "")
            line = f"- [{stype}] {reason}"
            if source:
                line += f" (source: {source})"
            lines.append(line)
    else:
        lines.append("- (no child branch suggestions emitted by reduction)")

    lines.extend(["", "## Lessons Surfaced"])
    if accepted_lessons:
        for lesson in accepted_lessons:
            lines.append(f"- {lesson}")
    else:
        lines.append("- (no lesson candidates accepted)")

    lines.extend(
        [
            "",
            "## Scope Of This Rebuttal",
            "- Per harness policy, the rebuttal stage runs only limited-depth, "
            "objection-linked checks; no new branches or scaleup experiments are "
            "introduced here.",
            "- Any further experiments must come from the orchestrator's child "
            "branch suggestions, not from this packet.",
        ]
    )

    text = "\n".join(lines) + "\n"
    output_path.write_text(text, encoding="utf-8")
    return text
