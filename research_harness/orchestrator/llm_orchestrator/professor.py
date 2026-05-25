"""Professor — the LLM that owns search policy.

Replaces the deterministic `reduce_node` function with an LLM call that
reads the claim contract, worker report, critic reviews, branch prior, and
the natural-language concerns the grad student raised, then produces a
structured decision:

    {
      "final_verdict": ...,
      "research_status": ...,
      "next_transition": "promoted" | "needs_child_branch" | "pruned",
      "child_branch_suggestions": [{type, reason, source, claim_focus}],
      "response_to_grad_student": "...natural language..."
    }

It also wraps the existing deterministic `reduce_node` as a safety net: if
the LLM response is missing or malformed the harness falls back to the
deterministic vending-machine so the tree never gets stuck.

The Professor never writes shared memory, never executes code, and never
takes a worker's claim mutation. Critics still run independently — Professor
just consumes them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from research_harness.orchestrator.llm_orchestrator.dialog import DialogEntry
from research_harness.orchestrator.llm_orchestrator.llm_client import LLMClient
from research_harness.orchestrator.reduction import reduce_node as _deterministic_reduce_node


VALID_TRANSITIONS = {"promoted", "needs_child_branch", "pruned"}
VALID_CHILD_TYPES = {
    "capability",
    "validity",
    "necessity",
    "boundary",
    "mechanism",
    "constraint",
    "taste",
    "operational",
}


# The persona is the load-bearing piece. Lazy advisors retreat to safe-but-weak
# claims to survive review; that produces papers nobody cares about. The harness
# explicitly forbids that retreat — every prompt the Professor sees opens with
# this persona block.
PROFESSOR_PERSONA = (
    "You are a senior research advisor (\"교수님\"). Failure modes you MUST avoid:\n"
    "  (1) LAZY HONESTY — narrowing the claim until it is trivially defensible. A "
    "claim that is technically true but explains nothing is a failure of taste.\n"
    "  (2) DISHONEST STRENGTH — overclaiming beyond evidence. Equally unacceptable.\n"
    "  (3) ONE-OFF CODE — when designing experiment code for a claim, do NOT write "
    "a throwaway script. Code you write must be REUSABLE by the next cohort of grad "
    "students testing successor claims (mechanism, necessity, boundary, …). Factor "
    "data generation, baselines, and metric computation into named modules with "
    "stable interfaces so the child node's experiment.py can `from <parent>.data "
    "import generate` rather than re-implement. A claim is testable by ONE "
    "experiment script; that one script should plug into a tree of reusable parts.\n"
    "Your job is to find HONEST + STRONG claims: the most ambitious claim the "
    "evidence supports. When in doubt, prefer keeping a strong claim and running "
    "one more focused experiment over weakening the claim. Refuse to weaken unless "
    "evidence specifically rules out the strong version.\n"
    "You own search policy. Workers (\"대학원생\") only execute experiments — they "
    "cannot mutate claims, baselines, or branches."
)


@dataclass
class Milestone:
    """One step in the Professor's research roadmap.

    Roadmaps are not rigid: each milestone carries an `expected_node_types`
    list so the harness knows when a milestone is satisfied, but the
    Professor is free to insert/skip/rewrite milestones mid-run as evidence
    accumulates (just like a real advisor revising the plan).
    """

    name: str
    description: str
    expected_node_types: list[str]
    status: str = "pending"  # pending | active | satisfied | dropped
    rationale: str = ""


@dataclass
class ResearchRoadmap:
    root_claim: str
    milestones: list[Milestone] = field(default_factory=list)
    revisions: list[dict[str, Any]] = field(default_factory=list)

    def active_milestone(self) -> Milestone | None:
        for m in self.milestones:
            if m.status == "active":
                return m
        for m in self.milestones:
            if m.status == "pending":
                return m
        return None

    def mark_satisfied(self, node_type: str) -> Milestone | None:
        active = self.active_milestone()
        if active and node_type in active.expected_node_types:
            active.status = "satisfied"
            self._activate_next()
            return active
        return None

    def _activate_next(self) -> None:
        for m in self.milestones:
            if m.status == "pending":
                m.status = "active"
                return

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_claim": self.root_claim,
            "milestones": [
                {
                    "name": m.name,
                    "description": m.description,
                    "expected_node_types": list(m.expected_node_types),
                    "status": m.status,
                    "rationale": m.rationale,
                }
                for m in self.milestones
            ],
            "revisions": list(self.revisions),
        }


@dataclass
class ProfessorDecision:
    reduction: dict[str, Any]
    response_to_grad_student: str
    dialog_entries: list[DialogEntry] = field(default_factory=list)
    # Follow-up claim children to spawn when the parent gets promoted.
    # The Professor uses these to ask the next grad student cohort
    # ("대학원생 1,2,3,4야") to test specific successor claims.
    follow_up_children: list[dict[str, Any]] = field(default_factory=list)
    # Roadmap edits the Professor wants to apply after this decision lands.
    # Each entry: {"op": "insert"|"drop"|"rewrite", "target": "...", "milestone": {...}}
    roadmap_edits: list[dict[str, Any]] = field(default_factory=list)


class Professor:
    """Reasoning-driven replacement for reduce_node."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def decide(
        self,
        *,
        node: dict[str, Any],
        worker_report: dict[str, Any],
        critic_reviews: list[dict[str, Any]],
        branch_prior: dict[str, Any] | None,
        grad_student_concerns: list[str] | None = None,
        roadmap: ResearchRoadmap | None = None,
    ) -> ProfessorDecision:
        deterministic = _deterministic_reduce_node(
            node, worker_report, critic_reviews, branch_prior=branch_prior
        )
        prompt = _build_professor_prompt(
            node=node,
            worker_report=worker_report,
            critic_reviews=critic_reviews,
            branch_prior=branch_prior,
            grad_student_concerns=grad_student_concerns or [],
            deterministic_baseline=deterministic,
            roadmap=roadmap,
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=(
                f"[intent:professor.reduce] {PROFESSOR_PERSONA}\n\n"
                "Read the worker report, critic reviews, and any grad student "
                "concerns. Produce a structured decision plus a short reply that "
                "reads like an advisor email."
            ),
            json_schema_hint=_PROFESSOR_RESPONSE_SCHEMA,
        )
        decision = _coerce_decision(response.structured, node, deterministic)
        is_mock = bool(response.raw.get("is_mock", False))
        dialog_entries = [
            DialogEntry(
                speaker="professor",
                intent="verdict",
                text=decision["response_to_grad_student"],
                metadata={
                    "final_verdict": decision["reduction"]["final_verdict"],
                    "next_transition": decision["reduction"]["next_transition"],
                    "follow_up_count": len(decision.get("follow_up_children") or []),
                    "is_mock": is_mock,
                },
            )
        ]
        # If the parent promoted, log the follow-up briefs as advisor messages
        # so dialog.json reads like an email thread launching the next cohort.
        for follow in decision.get("follow_up_children") or []:
            dialog_entries.append(
                DialogEntry(
                    speaker="professor",
                    intent="follow_up_brief",
                    text=(
                        f"Next cohort: please test the {follow['type']} successor — "
                        f"\"{follow['successor_claim']}\". "
                        f"Rationale: {follow.get('rationale', '')}"
                    ),
                    metadata={
                        "type": follow["type"],
                        "successor_claim": follow["successor_claim"],
                    },
                )
            )
        return ProfessorDecision(
            reduction=decision["reduction"],
            response_to_grad_student=decision["response_to_grad_student"],
            dialog_entries=dialog_entries,
            follow_up_children=decision.get("follow_up_children") or [],
        )

    def design_experiment(
        self,
        *,
        node: dict[str, Any],
        shared_lib_modules: list[str] | None = None,
        shared_lib_module_name: str | None = None,
    ) -> tuple[dict[str, Any], list[DialogEntry]]:
        """Design experiment code for this claim, reusing the shared lib.

        Strategy enforced by persona:
          - If `shared_lib_modules` lists modules already available under the
            thread's _lib/ (data.py, baselines/, eval/), this method should
            produce JUST a thin experiment.py that imports them. Don't
            duplicate logic.
          - If the shared lib doesn't yet exist (root node, first design call),
            produce the full reusable skeleton (data + baselines + eval +
            experiment.py) so successor claims can import.
          - If the shared lib is *partially* sufficient (some module needed
            but missing), produce experiment.py + the missing module only.

        Returns (plan_metadata, dialog_entries). plan_metadata's source_files
        are paths the harness will materialize. Paths starting with `_lib/`
        are written to the THREAD's shared lib (visible to all nodes). Paths
        without the prefix go under the per-node template dir.
        """
        contract = node["claim_contract"]
        shared = shared_lib_modules or []
        prompt = (
            "Design experiment code for the following claim. The thread shares a "
            "reusable `_lib/` package across all nodes — reuse it whenever you "
            "can. ONLY add new modules when the shared lib genuinely lacks what "
            "you need.\n\n"
            f"Claim under test:\n  {contract['claim_under_test']}\n\n"
            f"Mandatory baselines:\n"
            + "\n".join(f"  - {b}" for b in contract['mandatory_baselines'])
            + f"\n\nSuccess criteria:\n"
            + "\n".join(f"  - {s}" for s in contract['success_criteria'])
            + f"\n\nDisproof conditions:\n"
            + "\n".join(f"  - {d}" for d in contract['disproof_conditions'])
            + f"\n\nShared lib already available: "
            + (f"\n  - " + "\n  - ".join(shared) if shared else "(empty — root node, you should populate it now)")
            + (
                f"\n\nImport from shared lib via `from {shared_lib_module_name} "
                f"import data, baselines, eval`. Only emit _lib/ files for things "
                f"truly missing.\n"
                if shared_lib_module_name else ""
            )
            + "\n\nReturn JSON. source_files paths starting with `_lib/` are "
            "written to the thread's shared package (visible to all nodes); other "
            "paths land under this node's own dir."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.design_experiment] {PROFESSOR_PERSONA}",
            json_schema_hint=_DESIGN_EXPERIMENT_SCHEMA,
        )
        structured = response.structured or {}
        plan_meta = _coerce_experiment_plan(structured, node)
        is_mock = bool(response.raw.get("is_mock", False))
        sf_paths = [sf["path"] for sf in plan_meta.get("source_files") or []]
        added_to_lib = [p for p in sf_paths if p.startswith("_lib/")]
        new_node_files = [p for p in sf_paths if not p.startswith("_lib/")]
        dialog = [
            DialogEntry(
                speaker="professor",
                intent="experiment_designed",
                text=(
                    f"Designed experiment for {node['id']}: "
                    f"reusing shared lib ({len(shared)} module(s) available); "
                    f"added {len(added_to_lib)} new shared module(s), "
                    f"wrote {len(new_node_files)} node-local file(s)."
                ),
                metadata={
                    "is_mock": is_mock,
                    "task_class": plan_meta["task_class"],
                    "shared_lib_reused": shared,
                    "shared_lib_added": added_to_lib,
                    "node_local_files": new_node_files,
                },
            )
        ]
        return plan_meta, dialog

    def prioritize_claims(
        self,
        *,
        candidate_nodes: list[dict[str, Any]],
        active_milestone: str | None = None,
    ) -> tuple[list[str], list[DialogEntry]]:
        """Order admissible claims by which the advisor wants tested first.

        Reasoning enforced by persona: don't dispatch all claims at once
        when many are on the frontier — that wastes tokens / compute. Pick
        the order that minimises wasted work given the active roadmap
        milestone. Returns (ordered_node_ids, dialog_entries).
        """
        if not candidate_nodes:
            return [], []
        if len(candidate_nodes) == 1:
            # No ordering needed; skip the LLM call.
            return [candidate_nodes[0]["id"]], []
        summary = [
            {
                "id": n["id"],
                "type": n["type"],
                "claim": (n.get("claim_contract", {}).get("claim_under_test") or "")[:200],
            }
            for n in candidate_nodes
        ]
        prompt = (
            "Several claims are admissible for the current stage. The lab "
            "doesn't have unlimited tokens — pick the order in which the "
            "next cohort of grad students should tackle them. Front-load the "
            "claims whose result would most reduce uncertainty for the "
            "active milestone; defer the ones that depend on a result you "
            "haven't seen yet.\n\n"
            f"Active milestone: {active_milestone or '(none)'}\n\n"
            f"Candidates:\n{json.dumps(summary, indent=2, ensure_ascii=False)}"
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.prioritize_claims] {PROFESSOR_PERSONA}",
            json_schema_hint='{"ordered_ids": ["..."], "rationale": "..."}',
        )
        structured = response.structured or {}
        raw = structured.get("ordered_ids") or []
        # Sanitize: keep only known ids, append any missing in their original order.
        candidate_ids = [n["id"] for n in candidate_nodes]
        ordered: list[str] = []
        seen: set[str] = set()
        for nid in raw if isinstance(raw, list) else []:
            if isinstance(nid, str) and nid in candidate_ids and nid not in seen:
                ordered.append(nid)
                seen.add(nid)
        for nid in candidate_ids:
            if nid not in seen:
                ordered.append(nid)
        rationale = str(structured.get("rationale") or "").strip()
        entry = DialogEntry(
            speaker="professor",
            intent="prioritized_candidates",
            text=(
                f"Picked order for {len(ordered)} admissible claim(s): "
                f"{', '.join(ordered[:5])}{'…' if len(ordered) > 5 else ''}. "
                f"{rationale}"
            ),
            metadata={"ordered_ids": ordered, "active_milestone": active_milestone},
        )
        return ordered, [entry]

    def draft_roadmap(
        self,
        *,
        root_claim: str,
        domain: str,
        node_type: str,
    ) -> tuple[ResearchRoadmap, list[DialogEntry]]:
        """Sketch a multi-milestone research plan for the root claim.

        Returns a `ResearchRoadmap` whose milestones cover the load-bearing
        claim axes the lab plans to chase (e.g. validity → capability →
        mechanism → necessity → boundary). The roadmap is *intent*, not a
        contract — the Professor can revise it mid-run via `revise_roadmap`.
        """
        prompt = (
            f"You are starting a new research program in the {domain} domain.\n"
            f"Initial node type guess: {node_type}\n"
            f"Root claim under test: {root_claim}\n\n"
            "Sketch a research ROADMAP — 3 to 6 milestones the lab will chase. "
            "Each milestone declares (a) name, (b) one-sentence description, "
            "(c) which claim types (capability, validity, necessity, "
            "mechanism, boundary, constraint, operational, taste) it would "
            "be evidenced by. Order them as a credible research arc: validity "
            "first if the methodology isn't established, capability before "
            "mechanism, mechanism before boundary/ablation. Avoid the lazy "
            "shortcut of one giant milestone."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.draft_roadmap] {PROFESSOR_PERSONA}",
            json_schema_hint=(
                '{"milestones": [{"name": "...", "description": "...", '
                '"expected_node_types": ["..."], "rationale": "..."}]}'
            ),
        )
        structured = response.structured or {}
        raw = structured.get("milestones") or []
        milestones: list[Milestone] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            description = str(entry.get("description") or "").strip()
            types = [
                t for t in entry.get("expected_node_types") or []
                if isinstance(t, str) and t in VALID_CHILD_TYPES
            ]
            if not name or not description or not types:
                continue
            milestones.append(
                Milestone(
                    name=name,
                    description=description,
                    expected_node_types=types,
                    status="pending",
                    rationale=str(entry.get("rationale") or ""),
                )
            )
        if not milestones:
            # Safe default arc — same shape Sakana's 4 stages encode.
            milestones = [
                Milestone("establish_validity", "Confirm the measurement methodology is sound.", ["validity"], "pending"),
                Milestone("demonstrate_capability", "Show the methodology beats the baseline triad on the headline metric.", ["capability"], "pending"),
                Milestone("isolate_mechanism", "Identify the load-bearing component.", ["mechanism"], "pending"),
                Milestone("test_necessity", "Show same-budget alternatives don't match.", ["necessity"], "pending"),
                Milestone("map_boundary", "Find where the claim breaks.", ["boundary", "constraint"], "pending"),
            ]
        milestones[0].status = "active"
        roadmap = ResearchRoadmap(root_claim=root_claim, milestones=milestones)
        entry = DialogEntry(
            speaker="professor",
            intent="roadmap_drafted",
            text=(
                "Research roadmap:\n"
                + "\n".join(
                    f"  {i+1}. {m.name} — {m.description} "
                    f"[{','.join(m.expected_node_types)}]"
                    for i, m in enumerate(milestones)
                )
            ),
            metadata={"milestone_count": len(milestones)},
        )
        return roadmap, [entry]

    def revise_roadmap(
        self,
        *,
        roadmap: ResearchRoadmap,
        trigger: str,
        recent_decisions: list[dict[str, Any]],
    ) -> tuple[ResearchRoadmap, list[DialogEntry]]:
        """Edit the roadmap mid-run.

        `trigger` is a short text reason ("AC rejected the paper", "Mechanism
        node was contradicted, mechanism milestone needs rewrite", ...). The
        Professor proposes a list of edits (insert/drop/rewrite) and the
        harness applies them. The original roadmap is preserved under
        `revisions`.
        """
        prompt = (
            "Your current research roadmap:\n"
            + json.dumps(roadmap.to_dict(), indent=2, ensure_ascii=False)
            + f"\n\nReason for revision: {trigger}\n"
            f"Recent decision summaries:\n"
            + json.dumps(recent_decisions[-5:], indent=2, ensure_ascii=False)
            + "\n\nPropose edits. Each edit is one of:\n"
            "  {\"op\": \"insert\", \"after\": \"<milestone name or null>\", \"milestone\": {...}}\n"
            "  {\"op\": \"drop\", \"target\": \"<milestone name>\"}\n"
            "  {\"op\": \"rewrite\", \"target\": \"<milestone name>\", \"milestone\": {...}}\n"
            "Keep the roadmap honest+strong: do not delete milestones just to "
            "speed up submission."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.revise_roadmap] {PROFESSOR_PERSONA}",
            json_schema_hint='{"edits": [...], "summary": "..."}',
        )
        structured = response.structured or {}
        edits = structured.get("edits") or []
        summary = str(structured.get("summary") or "").strip()
        before = roadmap.to_dict()
        if isinstance(edits, list):
            for edit in edits:
                if not isinstance(edit, dict):
                    continue
                _apply_roadmap_edit(roadmap, edit)
        roadmap.revisions.append(
            {
                "trigger": trigger,
                "summary": summary,
                "edits": edits if isinstance(edits, list) else [],
                "before": before,
            }
        )
        entry = DialogEntry(
            speaker="professor",
            intent="roadmap_revised",
            text=summary or f"Roadmap revised due to: {trigger}",
            metadata={"trigger": trigger, "edit_count": len(edits) if isinstance(edits, list) else 0},
        )
        return roadmap, [entry]

    def problem_to_initial_claim(
        self,
        *,
        problem_statement: str,
        domain: str,
        node_type: str,
        goal_facets: list[str],
    ) -> tuple[dict[str, Any], list[DialogEntry]]:
        """Turn the intake agent's problem statement into a real claim_contract.

        The user (intake) hands the lab a problem like "the user wants X but
        can't tell Y from Z". The Professor now designs the actual research
        program: an honest+strong claim, a mandatory baseline triad, success
        criteria, and disproof conditions. Returns (claim_contract, dialog).
        """
        prompt = (
            "A funding agency (\"정출연\") has handed your lab a problem.\n\n"
            f"Problem statement: {problem_statement}\n"
            f"Domain: {domain}\n"
            f"Initial node type guess from intake: {node_type}\n"
            f"Goal facets: {goal_facets}\n\n"
            "The user has NOT proposed any method, baseline, or metric — that's "
            "YOUR job. Design the strongest honest claim_contract you can "
            "defend in front of a top-conference area chair. The output schema "
            "is:\n"
            "  claim_under_test: a single sentence that names what you are "
            "going to prove. Do NOT weaken it to a safe tautology.\n"
            "  mandatory_baselines: exactly three strings, one each for "
            "current_best_known / naive / random_or_null, NAMED concretely "
            "(e.g. \"current_best: Rolling-IC with autocorrelation gate\"). "
            "Do not say TBD.\n"
            "  success_criteria: 3-5 measurable criteria.\n"
            "  disproof_conditions: 3-5 specific outcomes that would falsify "
            "the claim.\n"
            "Refuse the lazy option of restating the problem as the claim."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.problem_to_claim] {PROFESSOR_PERSONA}",
            json_schema_hint=(
                '{"claim_under_test": "...", '
                '"mandatory_baselines": ["current_best: ...", "naive: ...", "random_or_null: ..."], '
                '"success_criteria": ["..."], '
                '"disproof_conditions": ["..."], '
                '"rationale": "<why this claim is HONEST + STRONG given the problem>"}'
            ),
        )
        structured = response.structured or {}
        claim_text = str(structured.get("claim_under_test") or problem_statement).strip()
        baselines = _coerce_string_list(structured.get("mandatory_baselines"), minimum=1)
        success = _coerce_string_list(structured.get("success_criteria"), minimum=1)
        disproof = _coerce_string_list(structured.get("disproof_conditions"), minimum=1)
        if not baselines:
            baselines = [
                "current_best: TBD",
                "naive: TBD",
                "random_or_null: TBD",
            ]
        if not success:
            success = ["Measurable success criterion to be set."]
        if not disproof:
            disproof = ["Outcome that would falsify the claim."]
        rationale = str(structured.get("rationale") or "").strip()
        entry = DialogEntry(
            speaker="professor",
            intent="problem_to_claim",
            text=(
                "Funding agency's problem received. Designing the claim:\n"
                f"  Claim: {claim_text}\n"
                f"  Rationale: {rationale or '(implicit)'}"
            ),
            metadata={"rationale": rationale, "problem_statement": problem_statement},
        )
        return (
            {
                "claim_under_test": claim_text,
                "mandatory_baselines": baselines,
                "success_criteria": success,
                "disproof_conditions": disproof,
            },
            [entry],
        )

    def decide_publication_readiness(
        self,
        *,
        root_claim: str,
        promoted_nodes: list[dict[str, Any]],
        pruned_nodes: list[dict[str, Any]],
        queued_count: int,
        cycle_index: int,
    ) -> tuple[bool, str, list[DialogEntry]]:
        """Decide whether the claim tree is mature enough to submit.

        Returns (ready_to_submit, advisor_message, dialog_entries).
        Not based on rounds — based on the *coverage* of the claim tree
        (mechanism / necessity / boundary axes hit) and the absence of
        unresolved blocking nodes.
        """
        coverage = sorted({n.get("type") for n in promoted_nodes if n.get("type")})
        prompt = (
            "Assess whether to submit this paper now.\n\n"
            f"Root claim: {root_claim}\n"
            f"Promoted claim types so far: {coverage}\n"
            f"Promoted count: {len(promoted_nodes)} | Pruned: {len(pruned_nodes)} | "
            f"Queued: {queued_count} | Cycle: {cycle_index}\n\n"
            "Decide either:\n"
            "  (a) SUBMIT — the claim tree has enough mechanism/necessity/boundary "
            "coverage to defend a STRONG version of the root claim.\n"
            "  (b) KEEP_WORKING — at least one critical axis is missing; describe "
            "which axis and why a lazy-weak rewrite would not be acceptable.\n"
            "Be specific. Do not submit a weak-but-safe claim."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.readiness] {PROFESSOR_PERSONA}",
            json_schema_hint=(
                '{"submit": true|false, "advisor_message": "...", '
                '"missing_axes": ["..."], "lazy_alternative_rejected": "..."}'
            ),
        )
        structured = response.structured or {}
        submit = bool(structured.get("submit", False))
        message = str(structured.get("advisor_message") or response.text or "").strip()
        if not message:
            message = (
                "Tree looks ready. Submitting." if submit else "Need at least one more axis covered."
            )
        return (
            submit,
            message,
            [
                DialogEntry(
                    speaker="professor",
                    intent="publication_readiness",
                    text=message,
                    metadata={
                        "submit": submit,
                        "missing_axes": list(structured.get("missing_axes") or []),
                        "coverage": coverage,
                    },
                )
            ],
        )

    def revise_after_ac_reject(
        self,
        *,
        root_claim: str,
        ac_decision: dict[str, Any],
        promoted_nodes: list[dict[str, Any]],
    ) -> tuple[str, list[DialogEntry]]:
        """When the AC rejects the paper, produce a NEW root claim.

        Strong, not safe. The advisor must keep ambition. The new claim should
        directly answer the AC's blocking objections without retreating to a
        trivially-true rewording.
        """
        blocking = ac_decision.get("blocking_reasons") or []
        scores = ac_decision.get("score_summary", {})
        prompt = (
            f"Our paper was REJECTED by the area chair.\n"
            f"Original root claim: {root_claim}\n"
            f"Promoted claim types: {sorted({n.get('type') for n in promoted_nodes if n.get('type')})}\n"
            f"AC scores: {scores}\n"
            f"Blocking reasons:\n" + "\n".join(f"  - {r}" for r in blocking) + "\n\n"
            "Propose a NEW root claim that:\n"
            "  (1) directly addresses the AC's blocking reasons,\n"
            "  (2) is HONEST given our actual evidence,\n"
            "  (3) is STRONG — not a defanged restatement that nobody would cite.\n"
            "If you find yourself writing a claim that's a tautological weakening, "
            "reject it and find a more focused but still ambitious successor."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.revise_after_reject] {PROFESSOR_PERSONA}",
            json_schema_hint='{"new_claim": "...", "rationale": "..."}',
        )
        structured = response.structured or {}
        new_claim = str(structured.get("new_claim") or "").strip()
        rationale = str(structured.get("rationale") or "").strip()
        if not new_claim:
            new_claim = root_claim  # absolutely no silent rewrite
            rationale = "AC reject without an honest stronger alternative; keeping original claim."
        return (
            new_claim,
            [
                DialogEntry(
                    speaker="professor",
                    intent="post_reject_revision",
                    text=(
                        f"AC rejected. Honest re-aim: {new_claim}\nRationale: {rationale}"
                    ),
                    metadata={"new_claim": new_claim, "rationale": rationale},
                )
            ],
        )

    def respond_to_concern(
        self,
        *,
        node: dict[str, Any],
        concern: str,
    ) -> tuple[str, list[DialogEntry]]:
        """Generate a natural-language response when the grad student flags a
        missing template / dataset / methodology gap. Returned as dialog text
        only — the actual template patching is logged for the operator to do.
        """
        prompt = (
            "A grad student working on the following claim raised a concern.\n"
            f"Claim: {node['claim_contract']['claim_under_test']}\n"
            f"Domain: {node.get('domain', 'n/a')}\n"
            f"Node type: {node['type']}\n\n"
            f"Concern from grad student:\n{concern}\n\n"
            "Reply as a senior advisor in 2-4 sentences. Either: "
            "(a) confirm you'll prepare the missing template/data and describe "
            "what you'll prepare, or (b) propose a scope narrowing that keeps "
            "the claim testable with available materials."
        )
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=f"[intent:professor.respond] {PROFESSOR_PERSONA}",
        )
        text = response.text.strip() or (
            "Acknowledged. I will prepare the missing materials and resubmit this node."
        )
        return text, [
            DialogEntry(
                speaker="professor",
                intent="response_to_concern",
                text=text,
            )
        ]


# --------------------------------------------------------------------------- #
# Prompt + schema                                                              #
# --------------------------------------------------------------------------- #


_DESIGN_EXPERIMENT_SCHEMA = """{
  "task_class": "smoke_test | eval | analysis | ablation | training",
  "objective": "<one-sentence description of what this code measures>",
  "entrypoint": {"command": ["python"], "args": ["src/experiment.py"]},
  "resources": {"timeout_sec": 60, "gpu": null, "cpu": 1, "memory_gb": 1},
  "expected_outputs": {
    "metrics_files": ["artifacts/metrics.json"],
    "logs": ["artifacts/run.log"],
    "artifact_dirs": ["artifacts/"]
  },
  "baseline_evidence_requirements": [
    {"role": "current_best_known|naive|random_or_null",
     "metric_key": "...", "baseline_key": "...",
     "operator": "greater_than|greater_equal|less_than|less_equal",
     "margin": 0, "required": true}
  ],
  "source_files": [
    {"path": "src/data.py", "purpose": "reusable data generator", "content": "<python source>"},
    {"path": "src/baselines/__init__.py", "purpose": "...", "content": "..."},
    {"path": "src/baselines/current_best.py", "purpose": "...", "content": "..."},
    {"path": "src/baselines/naive.py", "purpose": "...", "content": "..."},
    {"path": "src/baselines/random_or_null.py", "purpose": "...", "content": "..."},
    {"path": "src/eval/__init__.py", "purpose": "...", "content": "..."},
    {"path": "src/eval/metrics.py", "purpose": "reusable metric helpers", "content": "..."},
    {"path": "src/experiment.py", "purpose": "thin orchestration script for THIS claim", "content": "..."}
  ]
}"""


_PROFESSOR_RESPONSE_SCHEMA = """{
  "reduction": {
    "final_verdict": "supported_with_scope_narrowing | confounded_or_not_evaluable | contradicted | inconclusive | not_evaluable",
    "research_status": "<short status string>",
    "next_transition": "promoted | needs_child_branch | pruned",
    "child_branch_suggestions": [
      {"type": "validity|necessity|mechanism|boundary|constraint|capability|taste|operational",
       "reason": "...",
       "source": "professor_decision",
       "claim_focus": "...what the child node should test..."}
    ],
    "accepted_lesson_candidates": ["..."]
  },
  "response_to_grad_student": "<2-4 sentences advising the grad student>",
  "follow_up_children": [
    {"type": "mechanism|necessity|boundary|constraint|validity|capability|taste|operational",
     "successor_claim": "<the specific NEW claim the next grad student should test>",
     "rationale": "<why this follow-up is interesting given the parent's success>"}
  ]
}"""


def _build_professor_prompt(
    *,
    node: dict[str, Any],
    worker_report: dict[str, Any],
    critic_reviews: list[dict[str, Any]],
    branch_prior: dict[str, Any] | None,
    grad_student_concerns: list[str],
    deterministic_baseline: dict[str, Any],
    roadmap: "ResearchRoadmap | None" = None,
) -> str:
    contract = node["claim_contract"]
    parts = [
        f"=== Node {node['id']} ({node['type']}, stage={node['stage']}) ===",
        f"Claim under test: {contract['claim_under_test']}",
        "Mandatory baselines:",
        *(f"  - {b}" for b in contract["mandatory_baselines"]),
        "Success criteria:",
        *(f"  - {c}" for c in contract["success_criteria"]),
        "Disproof conditions:",
        *(f"  - {d}" for d in contract["disproof_conditions"]),
        "",
        "=== Worker report ===",
        f"Status: {worker_report.get('status')}",
        f"Claim verdict candidate: {worker_report.get('claim_verdict_candidate')}",
        f"Metrics: {json.dumps(worker_report.get('metrics') or {}, ensure_ascii=False)}",
        f"Baselines: {json.dumps(worker_report.get('baselines') or {}, ensure_ascii=False)}",
        f"Baseline evidence overall: {(worker_report.get('baseline_evidence_status') or {}).get('overall', 'n/a')}",
        f"Disproof conditions hit: {worker_report.get('disproof_conditions_hit') or []}",
    ]
    obs = worker_report.get("unexpected_observations") or []
    if obs:
        parts.append("Unexpected observations:")
        for o in obs:
            parts.append(f"  - {o.get('observation', '')} (evidence: {o.get('evidence', '')[:200]})")

    parts.append("\n=== Critic reviews ===")
    for review in critic_reviews:
        scores = review.get("scores", {})
        parts.append(
            f"  - {review.get('critic_id')}: verdict={review.get('verdict_candidate')}, "
            f"blocking={review.get('blocking')}, scores={scores}"
        )
        for objection in review.get("objections", []):
            parts.append(f"      objection: {objection.get('objection', '')}")

    if branch_prior:
        controls = branch_prior.get("risk_controls") or []
        if controls:
            parts.append("\n=== Failure-memory risk controls to consider ===")
            for ctrl in controls:
                parts.append(f"  - {ctrl.get('lesson', '')} ({ctrl.get('required_control', '')})")

    if grad_student_concerns:
        parts.append("\n=== Grad student raised these natural-language concerns ===")
        for concern in grad_student_concerns:
            parts.append(f"  - {concern}")

    if roadmap is not None:
        active = roadmap.active_milestone()
        parts.append(
            "\n=== Current research roadmap ===\n"
            + json.dumps(roadmap.to_dict()["milestones"], indent=2, ensure_ascii=False)
        )
        if active:
            parts.append(
                f"\nActive milestone: {active.name} — {active.description} "
                f"(expects node types: {active.expected_node_types})"
            )

    parts.append(
        "\n=== Deterministic baseline decision (for reference) ===\n"
        + json.dumps(deterministic_baseline, indent=2, ensure_ascii=False)
    )
    parts.append(
        "\nDecide the reduction. Be concrete about which child nodes to spawn and "
        "what each child should specifically test. Keep response_to_grad_student "
        "short (2-4 sentences), in the tone of an email reply."
    )
    return "\n".join(parts)


def _coerce_experiment_plan(
    structured: dict[str, Any], node: dict[str, Any]
) -> dict[str, Any]:
    """Coerce LLM output into a user_experiment_plan_metadata-shaped dict."""
    task_class = str(structured.get("task_class") or "smoke_test")
    if task_class not in {"smoke_test", "ablation", "training", "eval", "analysis"}:
        task_class = "smoke_test"
    objective = str(
        structured.get("objective")
        or f"Test {node['type']} claim {node['id']}"
    )
    entry = structured.get("entrypoint") or {}
    command = entry.get("command") or ["python"]
    args = entry.get("args") or ["src/experiment.py"]
    resources = structured.get("resources") or {}
    timeout_sec = int(resources.get("timeout_sec") or 60)
    expected_outputs = structured.get("expected_outputs") or {
        "metrics_files": ["artifacts/metrics.json"],
        "logs": ["artifacts/run.log"],
        "artifact_dirs": ["artifacts/"],
    }
    requirements = structured.get("baseline_evidence_requirements") or []
    if not requirements:
        # Default triad targeting a generic numeric metric.
        for role in ("current_best_known", "naive", "random_or_null"):
            requirements.append(
                {
                    "role": role,
                    "metric_key": "headline_metric",
                    "baseline_key": role,
                    "operator": "greater_than",
                    "margin": 0,
                    "required": True,
                }
            )
    source_files = []
    for sf in structured.get("source_files") or []:
        if not isinstance(sf, dict):
            continue
        path = str(sf.get("path") or "").strip()
        content = str(sf.get("content") or "")
        purpose = str(sf.get("purpose") or "Professor-designed module")
        if not path or not content:
            continue
        source_files.append({"path": path, "purpose": purpose, "content": content})
    return {
        "task_class": task_class,
        "objective": objective,
        "entrypoint": {"command": list(command), "args": list(args)},
        "resources": {"timeout_sec": timeout_sec, "gpu": None, "cpu": 1, "memory_gb": 1},
        "inputs": {"datasets": [], "snapshots": []},
        "expected_outputs": expected_outputs,
        "baseline_evidence_requirements": requirements,
        "source_files": source_files,
    }


def _apply_roadmap_edit(roadmap: ResearchRoadmap, edit: dict[str, Any]) -> None:
    op = str(edit.get("op") or "").lower()
    if op == "drop":
        target = str(edit.get("target") or "")
        for m in roadmap.milestones:
            if m.name == target and m.status != "satisfied":
                m.status = "dropped"
    elif op == "rewrite":
        target = str(edit.get("target") or "")
        spec = edit.get("milestone") or {}
        for m in roadmap.milestones:
            if m.name == target:
                if "name" in spec:
                    m.name = str(spec["name"])
                if "description" in spec:
                    m.description = str(spec["description"])
                if isinstance(spec.get("expected_node_types"), list):
                    m.expected_node_types = [
                        t for t in spec["expected_node_types"]
                        if isinstance(t, str) and t in VALID_CHILD_TYPES
                    ] or m.expected_node_types
                if "rationale" in spec:
                    m.rationale = str(spec["rationale"])
    elif op == "insert":
        spec = edit.get("milestone") or {}
        if not isinstance(spec, dict):
            return
        name = str(spec.get("name") or "").strip()
        description = str(spec.get("description") or "").strip()
        types = [
            t for t in spec.get("expected_node_types") or []
            if isinstance(t, str) and t in VALID_CHILD_TYPES
        ]
        if not name or not description or not types:
            return
        new_m = Milestone(
            name=name,
            description=description,
            expected_node_types=types,
            status="pending",
            rationale=str(spec.get("rationale") or ""),
        )
        after = edit.get("after")
        if not after:
            roadmap.milestones.append(new_m)
            return
        for idx, m in enumerate(roadmap.milestones):
            if m.name == after:
                roadmap.milestones.insert(idx + 1, new_m)
                return
        roadmap.milestones.append(new_m)


def _coerce_string_list(value: Any, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, list):
        return []
    result = [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
    return result if len(result) >= minimum else result


def _coerce_decision(
    structured: dict[str, Any] | None,
    node: dict[str, Any],
    deterministic: dict[str, Any],
) -> dict[str, Any]:
    """Validate / repair the LLM's structured response.

    Falls back to the deterministic reduction for any missing or invalid
    field so the tree always moves forward.
    """
    structured = structured or {}
    reduction_raw = structured.get("reduction") or {}
    next_transition = str(reduction_raw.get("next_transition") or deterministic["next_transition"])
    if next_transition not in VALID_TRANSITIONS:
        next_transition = deterministic["next_transition"]

    final_verdict = str(reduction_raw.get("final_verdict") or deterministic["final_verdict"])
    research_status = str(reduction_raw.get("research_status") or deterministic["research_status"])

    suggestions_raw = reduction_raw.get("child_branch_suggestions")
    if isinstance(suggestions_raw, list) and suggestions_raw:
        cleaned_suggestions: list[dict[str, Any]] = []
        for s in suggestions_raw:
            if not isinstance(s, dict):
                continue
            child_type = str(s.get("type") or "")
            if child_type not in VALID_CHILD_TYPES:
                continue
            cleaned_suggestions.append(
                {
                    "type": child_type,
                    "reason": str(s.get("reason") or "Professor-issued child branch."),
                    "source": str(s.get("source") or "professor_decision"),
                    "claim_focus": str(s.get("claim_focus") or ""),
                }
            )
        if not cleaned_suggestions:
            cleaned_suggestions = deterministic.get("child_branch_suggestions") or []
    else:
        cleaned_suggestions = deterministic.get("child_branch_suggestions") or []

    lessons_raw = reduction_raw.get("accepted_lesson_candidates")
    if isinstance(lessons_raw, list):
        lessons = [str(item) for item in lessons_raw if isinstance(item, str)]
    else:
        lessons = list(deterministic.get("accepted_lesson_candidates") or [])

    reduction = {
        **deterministic,
        "final_verdict": final_verdict,
        "research_status": research_status,
        "next_transition": next_transition,
        "child_branch_suggestions": cleaned_suggestions,
        "accepted_lesson_candidates": lessons,
    }
    response = str(structured.get("response_to_grad_student") or "").strip()
    if not response:
        response = (
            f"Reviewed your run on node {node['id']}. Verdict: {final_verdict}; "
            f"next step: {next_transition}."
        )
    follow_ups_raw = structured.get("follow_up_children")
    follow_ups: list[dict[str, Any]] = []
    if isinstance(follow_ups_raw, list):
        for entry in follow_ups_raw:
            if not isinstance(entry, dict):
                continue
            ftype = str(entry.get("type") or "")
            successor = str(entry.get("successor_claim") or "").strip()
            if ftype not in VALID_CHILD_TYPES or not successor:
                continue
            follow_ups.append(
                {
                    "type": ftype,
                    "successor_claim": successor,
                    "rationale": str(entry.get("rationale") or ""),
                }
            )
    return {
        "reduction": reduction,
        "response_to_grad_student": response,
        "follow_up_children": follow_ups,
    }
