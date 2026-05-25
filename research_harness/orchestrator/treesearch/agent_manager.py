"""AgentManager — Sakana parity in shape, claim-typed stages inside.

Sakana's AgentManager runs 4 stages with metric-improvement exit predicates:
    1. initial_implementation
    2. baseline_tuning
    3. creative_research
    4. ablation_studies

Ours runs 4 claim-typed stages with verdict/coverage exit predicates:
    1. scope_pinning           — admit {validity, taste, operational}
    2. baseline_evidence       — admit {capability}
    3. mechanism_or_necessity  — admit {mechanism, necessity}
    4. boundary_ablation       — admit {boundary, constraint}

Stage advancement is gated on Journal state (claim verdicts, coverage by
node_type), never on a numeric metric. Workers stay bounded; critics stay
read-only; live Claude stays off-tree behind live_dispatch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from research_harness.config import load_harness_config, load_settings
from research_harness.orchestrator.search_state import (
    initialize_search_state,
    search_policy_from_config,
    validate_search_state,
)
from research_harness.orchestrator.llm_orchestrator import (
    GradStudent,
    MockLLMClient,
    Professor,
    build_llm_client,
)
from research_harness.orchestrator.llm_orchestrator.mock_handlers import (
    register_default_mock_handlers,
)
from research_harness.orchestrator.treesearch.drafts import seed_drafts_from_root
from research_harness.orchestrator.treesearch.journal import Journal
from research_harness.orchestrator.treesearch.parallel_agent import ParallelAgent


logger = logging.getLogger(__name__)


StepCallback = Callable[[dict[str, Any]], None]


@dataclass
class Stage:
    """Sakana parity: one entry in the outer loop's sub-stage list.

    Differences from Sakana's Stage:
        - `goals` becomes `claim_types_admitted` (a set of node_type strings).
        - `max_iterations` is a hard cap on ParallelAgent.step() calls.
        - `exit_predicate` returns True when the stage's purpose is met,
          consulting Journal coverage rather than a numeric metric.
    """

    name: str
    description: str
    claim_types_admitted: set[str]
    max_iterations: int
    num_workers: int = 1
    stage_number: int = 0
    exit_predicate: Callable[[Journal], bool] | None = None


@dataclass
class StageTransition:
    from_stage: str
    to_stage: str
    reason: str
    iterations_used: int
    promoted_node_ids: list[str] = field(default_factory=list)


DEFAULT_STAGES_SPEC: list[dict[str, Any]] = [
    {
        "name": "scope_pinning",
        "description": (
            "Pin the claim scope. Resolve baseline triad placeholders and "
            "any validity/taste/operational gaps before capability work runs."
        ),
        "claim_types_admitted": {"validity", "taste", "operational"},
        "stage_number": 1,
    },
    {
        "name": "baseline_evidence",
        "description": (
            "Run the capability claim against current_best / naive / random. "
            "Advance when at least one capability node is promoted."
        ),
        "claim_types_admitted": {"capability"},
        "stage_number": 2,
    },
    {
        "name": "mechanism_or_necessity",
        "description": (
            "Branch into mechanism (why) and necessity (against same-budget "
            "baselines). Advance when at least one of each type is promoted "
            "OR exhausted."
        ),
        "claim_types_admitted": {"mechanism", "necessity"},
        "stage_number": 3,
    },
    {
        "name": "boundary_ablation",
        "description": (
            "Boundary and constraint children of the supported mechanism. "
            "Advance when the frontier is empty for this stage."
        ),
        "claim_types_admitted": {"boundary", "constraint"},
        "stage_number": 4,
    },
]


class AgentManager:
    """Outer 4-stage loop, Sakana-shaped, claim-typed inside.

    Public surface (Sakana parity):
        __init__(task_desc, cfg, workspace_dir)  — task_desc is a JSON string
            shaped like our refined_research_plan (claim_under_test, baselines,
            success_criteria, disproof_conditions), not Sakana's
            (Title/Abstract/Hypothesis/...).
        run(exec_callback=None, step_callback=None) -> dict
            Returns the final journal summary.
        current_stage / stages / journals / stage_history
    """

    def __init__(
        self,
        task_desc: str | dict[str, Any],
        cfg: dict[str, Any],
        workspace_dir: Path,
        *,
        repo_root: Path | None = None,
        root_node: dict[str, Any] | None = None,
    ) -> None:
        self.task_desc = self._parse_task_desc(task_desc)
        self.cfg = cfg
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = (repo_root or _infer_repo_root()).resolve()
        self.settings = load_settings(self.repo_root)
        self.policy = search_policy_from_config(self.repo_root)

        self.current_stage_number = 0
        self.stages: list[Stage] = list(self._build_stages())
        self.current_stage: Stage | None = None
        self.stage_history: list[StageTransition] = []
        self.completed_stages: list[str] = []

        if root_node is None:
            from research_harness.orchestrator.demo import _demo_node

            root_node = _demo_node()
        self.search_state = initialize_search_state(
            search_id=f"s_{self.task_desc.get('plan_id', 'agent_manager_run')}",
            root_node=root_node,
            policy=self.policy,
        )
        self.search_state["status"] = "running"

        search_cfg = (self.cfg or {}).get("search", {}) or {}
        num_drafts = int(search_cfg.get("num_drafts", 3))
        self.draft_node_ids = seed_drafts_from_root(
            self.search_state,
            num_drafts=num_drafts,
            max_depth=int(self.policy["max_depth"]),
        )
        self.journal = Journal.from_search_state(self.search_state)
        self.journals: dict[str, Journal] = {"main": self.journal}

        # Optional LLM orchestrator (Professor + GradStudent). Enabled when
        # settings.runtime.llm_orchestrator.enabled is true; defaults to mock.
        llm_cfg = (self.settings.get("runtime", {}) or {}).get("llm_orchestrator", {}) or {}
        self.professor: Professor | None = None
        self.grad_student: GradStudent | None = None
        if llm_cfg.get("enabled", False):
            prof_client = build_llm_client(self.settings, role="professor")
            grad_client = build_llm_client(self.settings, role="grad_student")
            if isinstance(prof_client, MockLLMClient):
                register_default_mock_handlers(prof_client)
            if isinstance(grad_client, MockLLMClient):
                register_default_mock_handlers(grad_client)
            self.professor = Professor(prof_client)
            self.grad_student = GradStudent(grad_client)
        self.readiness_history: list[dict[str, Any]] = []
        # Roadmap: Professor's living research plan. Drafted on first cycle,
        # revised when triggers fire (AC reject, contradicted node, etc.).
        self.roadmap = None
        if self.professor is not None:
            from research_harness.orchestrator.demo import _demo_node  # type: ignore  # noqa: F401

            root = next(
                (n for n in self.search_state["nodes"] if n.get("parent") is None),
                None,
            )
            if root is not None:
                self.roadmap, entries = self.professor.draft_roadmap(
                    root_claim=root["claim_contract"]["claim_under_test"],
                    domain=str(root.get("domain", "")),
                    node_type=str(root.get("type", "validity")),
                )
                self.roadmap_dialog_entries = [
                    {"speaker": e.speaker, "intent": e.intent, "text": e.text, "metadata": e.metadata}
                    for e in entries
                ]
            else:
                self.roadmap_dialog_entries = []
        else:
            self.roadmap_dialog_entries = []

    def _root_claim(self) -> str:
        for raw in self.search_state.get("nodes", []):
            if raw.get("parent") is None:
                return str(raw.get("claim_contract", {}).get("claim_under_test", ""))
        return ""

    def _parse_task_desc(self, task_desc: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(task_desc, str):
            return json.loads(task_desc)
        return dict(task_desc)

    def _build_stages(self) -> list[Stage]:
        stages_cfg = (self.cfg or {}).get("stage_configs", {}) or {}
        agent_cfg = (self.cfg or {}).get("agent", {}) or {}
        default_max_iters = int(agent_cfg.get("steps", 12))
        default_workers = int(agent_cfg.get("num_workers", 1))
        stages: list[Stage] = []
        for spec in DEFAULT_STAGES_SPEC:
            stage_cfg = stages_cfg.get(spec["name"], {}) or {}
            stages.append(
                Stage(
                    name=spec["name"],
                    description=spec["description"],
                    claim_types_admitted=set(spec["claim_types_admitted"]),
                    max_iterations=int(stage_cfg.get("max_iterations", default_max_iters)),
                    num_workers=int(stage_cfg.get("num_workers", default_workers)),
                    stage_number=spec["stage_number"],
                    exit_predicate=self._default_exit_predicate(spec["name"]),
                )
            )
        return stages

    @staticmethod
    def _default_exit_predicate(stage_name: str) -> Callable[[Journal], bool]:
        if stage_name == "scope_pinning":
            def _scope_done(j: Journal) -> bool:
                coverage = j.coverage_by_node_type()
                return any(t in coverage for t in ("validity", "taste", "operational")) or _no_queued_for_types(
                    j, {"validity", "taste", "operational"}
                )

            return _scope_done
        if stage_name == "baseline_evidence":
            def _baseline_done(j: Journal) -> bool:
                coverage = j.coverage_by_node_type()
                return bool(coverage.get("capability")) or _no_queued_for_types(j, {"capability"})

            return _baseline_done
        if stage_name == "mechanism_or_necessity":
            def _mech_done(j: Journal) -> bool:
                coverage = j.coverage_by_node_type()
                return (
                    bool(coverage.get("mechanism")) or bool(coverage.get("necessity"))
                ) or _no_queued_for_types(j, {"mechanism", "necessity"})

            return _mech_done
        # boundary_ablation
        def _boundary_done(j: Journal) -> bool:
            return _no_queued_for_types(j, {"boundary", "constraint"})

        return _boundary_done

    def run(
        self,
        *,
        step_callback: StepCallback | None = None,
        stage_callback: Callable[[Stage, list[dict[str, Any]]], None] | None = None,
        max_cycles: int = 8,
    ) -> dict[str, Any]:
        """Run the 4 claim-typed stages in order, cycling until queue is empty.

        Each cycle steps through scope_pinning → baseline_evidence →
        mechanism_or_necessity → boundary_ablation. Within a stage,
        ParallelAgent.step is called until max_iterations or the
        exit_predicate trips. After all four, if any node remains queued
        and admissible, the cycle repeats up to `max_cycles` times — this
        is how children produced late in cycle N get serviced in cycle N+1.
        """
        run_dir = self.workspace_dir
        for cycle in range(max(1, int(max_cycles))):
            cycle_progress = False
            for stage in self.stages:
                self.current_stage = stage
                self.current_stage_number = stage.stage_number
                search_cfg = (self.cfg or {}).get("search", {}) or {}
                agent_cfg = (self.cfg or {}).get("agent", {}) or {}
                agent = ParallelAgent(
                    journal=self.journal,
                    repo_root=self.repo_root,
                    run_dir=run_dir,
                    settings=self.settings,
                    num_workers=stage.num_workers,
                    admits_node_types=stage.claim_types_admitted,
                    debug_prob=float(search_cfg.get("debug_prob", 0.0)),
                    max_debug_depth=int(self.policy["max_debug_depth"]),
                    num_seeds=int(agent_cfg.get("num_seeds", 1)),
                    professor=self.professor,
                    grad_student=self.grad_student,
                    roadmap=self.roadmap,
                )
                iterations_used = 0
                stage_summaries: list[dict[str, Any]] = []
                for _ in range(stage.max_iterations):
                    summaries = agent.step(callback=step_callback)
                    if not summaries:
                        break
                    stage_summaries.extend(summaries)
                    iterations_used += 1
                    cycle_progress = True
                    if stage.exit_predicate and stage.exit_predicate(self.journal):
                        break

                promoted_ids = self.journal.coverage_by_node_type()
                promoted_flat = sorted(
                    {
                        nid
                        for t in stage.claim_types_admitted
                        for nid in promoted_ids.get(t, [])
                    }
                )
                self.stage_history.append(
                    StageTransition(
                        from_stage=f"cycle{cycle + 1}:{stage.name}",
                        to_stage=self._next_stage_name(stage),
                        reason=self._transition_reason(stage, iterations_used),
                        iterations_used=iterations_used,
                        promoted_node_ids=promoted_flat,
                    )
                )
                if cycle == 0:
                    self.completed_stages.append(stage.name)
                if stage_callback is not None:
                    stage_callback(stage, stage_summaries)

            # Professor decides whether the claim tree is ready for paper
            # submission. Not round-based — based on coverage of the
            # critical successor axes. The persona enforces honesty + strength.
            if self.professor is not None:
                ready, message, entries = self.professor.decide_publication_readiness(
                    root_claim=self._root_claim(),
                    promoted_nodes=[
                        raw for raw in self.journal.search_state["nodes"]
                        if raw["id"] in self.journal.promotion_order
                    ],
                    pruned_nodes=[
                        raw for raw in self.journal.search_state["nodes"]
                        if raw["status"] == "pruned"
                    ],
                    queued_count=sum(
                        1 for item in self.journal.search_state["frontier"]
                        if item["status"] == "queued"
                    ),
                    cycle_index=cycle + 1,
                )
                self.readiness_history.append(
                    {
                        "cycle": cycle + 1,
                        "submit": ready,
                        "message": message,
                        "dialog_entries": [
                            {
                                "speaker": e.speaker,
                                "intent": e.intent,
                                "text": e.text,
                                "metadata": e.metadata,
                            }
                            for e in entries
                        ],
                    }
                )
                if ready:
                    break
            if not _any_queued(self.journal):
                break
            if not cycle_progress:
                break

        validate_search_state(self.journal.search_state)
        self.journal.search_state["status"] = (
            "completed"
            if not _any_queued(self.journal)
            else "blocked"
        )
        return self._final_summary()

    def _next_stage_name(self, stage: Stage) -> str:
        idx = self.stages.index(stage)
        if idx + 1 >= len(self.stages):
            return "publish"
        return self.stages[idx + 1].name

    @staticmethod
    def _transition_reason(stage: Stage, iterations_used: int) -> str:
        return (
            f"stage {stage.name} ran {iterations_used}/{stage.max_iterations} iterations; "
            f"admitted types {sorted(stage.claim_types_admitted)}"
        )

    def _final_summary(self) -> dict[str, Any]:
        return {
            "search_state": self.journal.search_state,
            "stage_history": [
                {
                    "from_stage": t.from_stage,
                    "to_stage": t.to_stage,
                    "reason": t.reason,
                    "iterations_used": t.iterations_used,
                    "promoted_node_ids": t.promoted_node_ids,
                }
                for t in self.stage_history
            ],
            "completed_stages": list(self.completed_stages),
            "coverage_by_node_type": self.journal.coverage_by_node_type(),
            "promoted_node_ids": list(self.journal.promotion_order),
            "readiness_history": list(self.readiness_history),
        }


def run_agent_manager_search(
    repo_root: Path,
    run_dir: Path,
    *,
    backend_name: str = "mock",
    root_node: dict[str, Any] | None = None,
    record_runner_failures: bool = True,
    step_callback: StepCallback | None = None,
) -> dict[str, Any]:
    """Drop-in replacement for run_mock_tree_search using AgentManager.

    Returns the same {"search_state": ..., "artifacts": [...]} shape and
    writes the same search_state.json + tree_search_summary.json files, so
    production_runner and any caller can swap implementations transparently.
    """
    if backend_name != "mock":
        raise ValueError(
            "tree search only accepts the mock backend; live Claude stays "
            "behind live_dispatch"
        )
    repo_root = repo_root.resolve()
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    harness_cfg = load_harness_config(repo_root)
    if root_node is None:
        from research_harness.orchestrator.demo import _demo_node

        root_node = _demo_node()
    task_desc = {
        "plan_id": f"agent_manager_{root_node['id']}",
        "claim_under_test": root_node["claim_contract"]["claim_under_test"],
    }
    manager = AgentManager(
        task_desc=task_desc,
        cfg=harness_cfg,
        workspace_dir=run_dir,
        repo_root=repo_root,
        root_node=root_node,
    )
    artifacts: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()

    def _record(summary: dict[str, Any]) -> None:
        nid = summary["node_id"]
        if nid in seen_node_ids:
            return
        seen_node_ids.add(nid)
        node = next(
            n for n in manager.journal.search_state["nodes"] if n["id"] == nid
        )
        outputs = node.get("outputs", {}) or {}
        artifacts.append(
            {
                "node_id": nid,
                "experiment_plan_path": outputs.get("experiment_plan_path"),
                "runner_result_path": outputs.get("runner_result_path"),
                "source_files": outputs.get("source_files", []),
                "metrics_evidence_paths": outputs.get("metrics_evidence_paths", []),
                "runner_status": outputs.get("runner_status"),
                "worker_status": summary["worker_status"],
                "next_transition": summary["next_transition"],
                "template_used": summary["template_used"],
            }
        )
        if step_callback is not None:
            step_callback(summary)

    manager.run(step_callback=_record)
    state = manager.journal.search_state
    validate_search_state(state)
    summary = {
        "search_state": state,
        "artifacts": artifacts,
        "stage_history": [
            {
                "from_stage": t.from_stage,
                "to_stage": t.to_stage,
                "reason": t.reason,
                "iterations_used": t.iterations_used,
                "promoted_node_ids": t.promoted_node_ids,
            }
            for t in manager.stage_history
        ],
        "completed_stages": list(manager.completed_stages),
        "coverage_by_node_type": manager.journal.coverage_by_node_type(),
        "readiness_history": list(manager.readiness_history),
    }
    _write_json(run_dir / "search_state.json", state)
    _write_json(run_dir / "tree_search_summary.json", summary)
    return summary


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _no_queued_for_types(journal: Journal, types: set[str]) -> bool:
    state = journal.search_state
    by_id = {raw["id"]: raw for raw in state["nodes"]}
    for item in state["frontier"]:
        if item["status"] != "queued":
            continue
        node_type = by_id.get(item["node_id"], {}).get("type")
        if node_type in types:
            return False
    return True


def _any_queued(journal: Journal) -> bool:
    return any(item["status"] == "queued" for item in journal.search_state["frontier"])


def _infer_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
