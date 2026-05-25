"""GradStudent — the LLM that runs experiments and reports back.

The grad student:
  1. Reads the worker_task contract and confirms the template is usable.
  2. If usable, runs the deterministic LocalRunner (workers do not execute
     LLM-authored code in the harness tree; that's still off-tree behind
     live_dispatch). The grad student attaches a natural-language commentary
     to the run.
  3. If the template/data is *not* usable, the grad student does NOT mutate
     scope. Instead it raises a `GradStudentConcern` that the Professor
     reads and responds to.

In every case the grad student stays within forbidden_actions:
  - cannot change claim_contract
  - cannot create child branches
  - cannot write shared memory
  - cannot write outside its workspace
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from research_harness.orchestrator.llm_orchestrator.dialog import (
    DialogEntry,
)
from research_harness.orchestrator.llm_orchestrator.llm_client import LLMClient


# The grad student is bounded by `forbidden_actions` (cannot create claims,
# branches, or write shared memory). Within those bounds, the harness wants
# the grad student to be SKEPTICAL — like a sharp PhD who is willing to push
# back on the advisor when the experimental setup looks too clean. Lazy
# compliance is itself a failure mode: a grad student who never says "잠깐만"
# never catches the synthetic-data leak that explains the headline number.
GRAD_STUDENT_PERSONA = (
    "You are a sharp graduate student (\"대학원생\"). You execute experiments "
    "that your advisor assigns; you do NOT propose new claims, change "
    "baselines, or create branches — that's the advisor's job and the harness "
    "enforces it.\n"
    "BUT you are paid to spot problems your advisor might miss. Be willing to "
    "push back. Examples of legitimate pushback:\n"
    "  - The synthetic data setup is too idealized to actually test the claim "
    "    (e.g. the generator and the evaluator share latent structure).\n"
    "  - The baseline definitions look conceptually unfair (e.g. baseline "
    "    given fewer features than the proposed method).\n"
    "  - The success criterion is measurable but irrelevant to the claim.\n"
    "  - The disproof condition is trivially unhittable.\n"
    "When you see this, raise a CONCERN to the advisor in natural language. "
    "Phrase it like an email: \"교수님, 이거 의문이 좀 있어요…\" Then describe "
    "the issue concretely. You're not refusing to run the experiment, you're "
    "flagging that the design itself may not test what the advisor thinks it "
    "tests. Better to flag now than waste a node on a setup that begs the "
    "question."
)


@dataclass
class GradStudentConcern:
    """Natural-language concern the grad student raised to the Professor.

    The harness translates this into `worker_report.unexpected_observations`
    so existing schemas stay intact.
    """

    summary: str
    evidence: str
    blocked: bool
    suggested_branch_type: str | None = None


@dataclass
class GradStudentReport:
    commentary: str
    concerns: list[GradStudentConcern] = field(default_factory=list)
    dialog_entries: list[DialogEntry] = field(default_factory=list)

    @property
    def has_blocking_concern(self) -> bool:
        return any(c.blocked for c in self.concerns)


class GradStudent:
    """LLM-driven commentary + concern reporter, bounded by worker_task."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def review_task_before_run(
        self,
        *,
        node: dict[str, Any],
        worker_task: dict[str, Any],
        domain_template_present: bool,
        domain_template_summary: str,
    ) -> GradStudentReport:
        """Look at the task brief and decide if the template is enough.

        Returns a report with optional concerns. Concerns flagged as
        blocked=True mean the Professor must respond before the run proceeds.
        """
        prompt = (
            f"You are a grad student reviewing a task brief from your advisor.\n\n"
            f"Claim under test:\n  {node['claim_contract']['claim_under_test']}\n\n"
            f"Mandatory baselines:\n"
            + "\n".join(f"  - {b}" for b in node['claim_contract']['mandatory_baselines'])
            + f"\n\nDomain template available: {'yes' if domain_template_present else 'no'}\n"
            f"Template summary: {domain_template_summary}\n\n"
            f"Forbidden actions: {worker_task.get('forbidden_actions', [])}\n\n"
            "Decide: (a) the template is enough to test the claim → respond with a "
            "1-2 sentence acknowledgement and no concerns, or (b) the template is "
            "missing something needed → respond with a 1-2 sentence summary AND "
            "list specific concerns. You may NOT propose new claims, change "
            "baselines, or create branches; you may only describe what's missing."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:grad_student.review] {GRAD_STUDENT_PERSONA}",
            json_schema_hint=_REVIEW_SCHEMA,
        )
        return _coerce_review_response(response, domain_template_present)

    def post_run_commentary(
        self,
        *,
        node: dict[str, Any],
        worker_report: dict[str, Any],
    ) -> GradStudentReport:
        """Write a short natural-language commentary on the run's outcome.

        This is the "수고하셨다" email the grad student sends after wrapping
        up an experiment: what they saw, what surprised them, what they'd
        want the advisor to weigh in on. It does NOT change any field on
        worker_report itself.
        """
        prompt = (
            "You are a grad student writing a short note to your advisor about "
            "the run that just finished.\n\n"
            f"Claim: {node['claim_contract']['claim_under_test']}\n"
            f"Worker status: {worker_report.get('status')}\n"
            f"Claim verdict candidate: {worker_report.get('claim_verdict_candidate')}\n"
            f"Metrics: {json.dumps(worker_report.get('metrics') or {}, ensure_ascii=False)}\n"
            f"Baseline status: {(worker_report.get('baseline_evidence_status') or {}).get('overall', 'n/a')}\n"
            f"Disproof hits: {worker_report.get('disproof_conditions_hit') or []}\n\n"
            "Write a 2-4 sentence email. Note anything you think the advisor "
            "should look at, but do NOT decide promotion or branching — that's "
            "the advisor's call."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:grad_student.commentary] {GRAD_STUDENT_PERSONA}",
        )
        text = response.text.strip() or _default_commentary(worker_report)
        is_mock = bool(response.raw.get("is_mock", False))
        return GradStudentReport(
            commentary=text,
            concerns=[],
            dialog_entries=[
                DialogEntry(
                    speaker="grad_student",
                    intent="post_run_commentary",
                    text=text,
                    metadata={
                        "worker_status": worker_report.get("status"),
                        "is_mock": is_mock,
                    },
                )
            ],
        )


# --------------------------------------------------------------------------- #
# Schema + coercion                                                            #
# --------------------------------------------------------------------------- #


_REVIEW_SCHEMA = """{
  "commentary": "<1-2 sentence acknowledgement to advisor>",
  "concerns": [
    {"summary": "...", "evidence": "...", "blocked": true|false,
     "suggested_branch_type": "validity|operational|null"}
  ]
}"""


def _coerce_review_response(
    response: Any,
    domain_template_present: bool,
) -> GradStudentReport:
    structured = response.structured if response.structured else {}
    is_mock = bool(response.raw.get("is_mock", False))
    commentary = str(structured.get("commentary") or response.text or "").strip()
    if not commentary:
        commentary = (
            "Template looks workable; proceeding with the run."
            if domain_template_present
            else "Template is missing; flagging a concern."
        )
    concerns_raw = structured.get("concerns") or []
    concerns: list[GradStudentConcern] = []
    for c in concerns_raw if isinstance(concerns_raw, list) else []:
        if not isinstance(c, dict):
            continue
        suggested = c.get("suggested_branch_type")
        if suggested in ("", None, "null"):
            suggested = None
        concerns.append(
            GradStudentConcern(
                summary=str(c.get("summary") or ""),
                evidence=str(c.get("evidence") or ""),
                blocked=bool(c.get("blocked")),
                suggested_branch_type=str(suggested) if suggested else None,
            )
        )
    if not domain_template_present and not concerns:
        # Hard floor: missing template ⇒ at least one concern.
        concerns.append(
            GradStudentConcern(
                summary="Domain template is missing.",
                evidence=(
                    "experiment_plan_templates/<domain>/ has no plan.json+src/ pair, "
                    "so the deterministic runner cannot materialize this experiment."
                ),
                blocked=True,
                suggested_branch_type="operational",
            )
        )
    return GradStudentReport(
        commentary=commentary,
        concerns=concerns,
        dialog_entries=[
            DialogEntry(
                speaker="grad_student",
                intent="task_review",
                text=commentary,
                metadata={
                    "domain_template_present": domain_template_present,
                    "concerns_raised": len(concerns),
                    "is_mock": is_mock,
                },
            )
        ],
    )


def _default_commentary(worker_report: dict[str, Any]) -> str:
    return (
        f"Run finished with status {worker_report.get('status')}; "
        f"verdict candidate {worker_report.get('claim_verdict_candidate')}."
    )
