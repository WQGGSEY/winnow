from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.memory.failure_memory import (
    record_failure_candidate,
    record_runner_failure_candidate,
)
from research_harness.orchestrator.branch_prior import build_failure_branch_prior
from research_harness.orchestrator.experiment_plan import (
    build_experiment_plan_for_node,
    build_job_manifest_from_experiment_plan,
    validate_experiment_plan,
)
from research_harness.orchestrator.reduction import reduce_node
from research_harness.orchestrator.search_state import (
    transition_node,
    validate_search_state,
)
from research_harness.orchestrator.llm_orchestrator import (
    DialogEntry,
    DialogLog,
    GradStudent,
    Professor,
)
from research_harness.orchestrator.tree_search import _attach_runner_outputs
from research_harness.orchestrator.treesearch.interpreter import Interpreter
from research_harness.orchestrator.treesearch.journal import Journal
from research_harness.runner.evidence import build_worker_report_from_runner_evidence
from research_harness.schemas.validator import validate_named_schema


logger = logging.getLogger(__name__)


StepCallback = Callable[[dict[str, Any]], None]


class ParallelAgent:
    """Run one or more queued frontier items through the per-node pipeline.

    Sakana parity:
        - num_workers concurrency (ThreadPoolExecutor)
        - step(callback) advances the journal by one batch
        - admits_node_type / stage scoping (set by AgentManager)

    Differences from Sakana:
        - no _draft / _debug / _improve LLM calls — the per-node pipeline is
          deterministic (LocalRunner + reduce_node), since claim verdicts come
          from runner evidence + critic blocking, not LLM scoring.
        - negative evidence closes the current direction; successor generation
          belongs to the blind sequential engine.
    """

    def __init__(
        self,
        *,
        journal: Journal,
        repo_root: Path,
        run_dir: Path,
        settings: dict[str, Any],
        num_workers: int = 1,
        admits_node_types: set[str] | None = None,
        record_runner_failures: bool = True,
        num_seeds: int = 1,
        professor: Professor | None = None,
        grad_student: GradStudent | None = None,
        roadmap: Any = None,
    ) -> None:
        self.journal = journal
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.settings = settings
        self.num_workers = max(1, int(num_workers))
        self.admits_node_types = admits_node_types
        self.record_runner_failures = record_runner_failures
        self.num_seeds = max(1, int(num_seeds))
        self.interpreter = Interpreter(run_dir=run_dir, settings=settings)
        self.professor = professor
        self.grad_student = grad_student
        self.roadmap = roadmap

    def step(self, callback: StepCallback | None = None) -> list[dict[str, Any]]:
        """Pop up to num_workers queued items and run their pipeline concurrently.

        Returns the list of per-node summaries (one per executed item).
        Each summary is also passed to `callback` if provided.
        """
        validate_search_state(self.journal.search_state)
        batch = self._select_parallel_nodes()
        if not batch:
            return []

        for item in batch:
            transition_node(
                self.journal.search_state,
                item["node_id"],
                "running",
                event="dequeue",
                reason="frontier selected by ParallelAgent",
            )

        summaries: list[dict[str, Any]] = []
        if self.num_workers == 1 or len(batch) == 1:
            for item in batch:
                summary = self._process_one(item)
                summaries.append(summary)
                if callback is not None:
                    callback(summary)
        else:
            with ThreadPoolExecutor(max_workers=self.num_workers) as pool:
                futures = {pool.submit(self._process_one, item): item for item in batch}
                for fut in as_completed(futures):
                    summary = fut.result()
                    summaries.append(summary)
                    if callback is not None:
                        callback(summary)

        return summaries

    def _select_parallel_nodes(self) -> list[dict[str, Any]]:
        """Sakana parity: _select_parallel_nodes. Claim-typed filter +
        Professor-driven ordering applied.

        If the Professor is enabled and there are multiple admissible
        candidates, ask it which to run first — saves grad-student tokens
        when the frontier is wide. Otherwise fall back to the deterministic
        priority/depth/id sort.
        """
        state = self.journal.search_state
        queued = [item for item in state["frontier"] if item["status"] == "queued"]
        by_id = {raw["id"]: raw for raw in state["nodes"]}
        if self.admits_node_types is not None:
            queued = [
                item
                for item in queued
                if by_id.get(item["node_id"], {}).get("type") in self.admits_node_types
            ]
        if not queued:
            return []
        if self.professor is not None and len(queued) > 1:
            active = (
                self.roadmap.active_milestone() if self.roadmap is not None else None
            )
            milestone_name = active.name if active else None
            candidate_nodes = [by_id[item["node_id"]] for item in queued]
            ordered_ids, _entries = self.professor.prioritize_claims(
                candidate_nodes=candidate_nodes,
                active_milestone=milestone_name,
            )
            order_index = {nid: i for i, nid in enumerate(ordered_ids)}
            queued.sort(
                key=lambda item: (
                    order_index.get(item["node_id"], len(ordered_ids)),
                    item["depth"],
                    item["node_id"],
                )
            )
        else:
            queued.sort(
                key=lambda item: (-item["priority"], item["depth"], item["node_id"])
            )
        return queued[: self.num_workers]

    def _process_one(self, frontier_item: dict[str, Any]) -> dict[str, Any]:
        state = self.journal.search_state
        node = self._node_by_id(state, frontier_item["node_id"])
        node_run_dir = self.run_dir / "nodes" / node["id"]
        node_run_dir.mkdir(parents=True, exist_ok=True)
        dialog = DialogLog(node_id=node["id"])

        if self.grad_student is not None and self.professor is not None:
            template_summary, template_present = self._template_summary(node)
            review = self.grad_student.review_task_before_run(
                node=node,
                worker_task=_lightweight_worker_task(node),
                domain_template_present=template_present,
                domain_template_summary=template_summary,
            )
            dialog.entries.extend(review.dialog_entries)
            for concern in review.concerns:
                dialog.append(
                    DialogEntry(
                        speaker="grad_student",
                        intent="concern",
                        text=f"{concern.summary} — {concern.evidence}",
                        metadata={"blocked": concern.blocked},
                    )
                )
                if concern.blocked:
                    response, prof_entries = self.professor.respond_to_concern(
                        node=node, concern=f"{concern.summary} — {concern.evidence}"
                    )
                    dialog.entries.extend(prof_entries)

        runner_summary = self._execute_runner(node, node_run_dir)
        worker_report = runner_summary["worker_report"]
        validate_named_schema("worker_report", worker_report)
        _write_json(node_run_dir / "worker_report.json", worker_report)

        if self.grad_student is not None:
            post = self.grad_student.post_run_commentary(
                node=node, worker_report=worker_report
            )
            dialog.entries.extend(post.dialog_entries)

        if (
            self.record_runner_failures
            and worker_report.get("failure_record_candidate")
            and runner_summary["runner_failure_memory"] is None
        ):
            record_failure_candidate(
                self.repo_root,
                node,
                worker_report,
                source_artifact=_worker_failure_source(worker_report),
            )

        transition_node(
            state,
            node["id"],
            "completed_worker_report",
            event="worker_report",
            reason=f"worker status: {worker_report['status']}",
        )

        critics = select_critics(self.repo_root, node)
        reviews = run_critic_reviews(node, worker_report, critics)
        for review in reviews:
            validate_named_schema("critic_review", review)
        _write_json(node_run_dir / "critic_reviews.json", reviews)
        transition_node(
            state,
            node["id"],
            "critic_reviewed",
            event="critic_reviews",
            reason=f"{len(reviews)} critics reviewed node",
        )

        branch_prior = build_failure_branch_prior(self.repo_root, node, self.settings)
        if self.professor is not None:
            grad_concerns = [
                e.text for e in dialog.entries
                if e.speaker == "grad_student" and e.intent == "concern"
            ]
            decision = self.professor.decide(
                node=node,
                worker_report=worker_report,
                critic_reviews=reviews,
                branch_prior=branch_prior,
                grad_student_concerns=grad_concerns,
                roadmap=self.roadmap,
            )
            reduction = decision.reduction
            dialog.entries.extend(decision.dialog_entries)
            # Mark roadmap milestone satisfied when the parent's claim axis was
            # validated. The Professor can still revise the roadmap later.
            if self.roadmap is not None and reduction["next_transition"] == "promoted":
                satisfied = self.roadmap.mark_satisfied(node["type"])
                if satisfied is not None:
                    dialog.append(
                        DialogEntry(
                            speaker="professor",
                            intent="milestone_satisfied",
                            text=(
                                f"Milestone '{satisfied.name}' satisfied by "
                                f"{node['id']} ({node['type']})."
                            ),
                            metadata={"milestone": satisfied.name},
                        )
                    )
        else:
            reduction = reduce_node(node, worker_report, reviews, branch_prior=branch_prior)
        _write_json(node_run_dir / "orchestrator_reduction.json", reduction)
        if dialog.entries:
            dialog.write(node_run_dir / "dialog.json")
        transition_node(
            state,
            node["id"],
            "orchestrator_reduced",
            event="orchestrator_reduction",
            reason=reduction["final_verdict"],
        )

        self.journal.record_reduction(node["id"], worker_report, reduction)

        if reduction["next_transition"] == "promoted":
            transition_node(
                state,
                node["id"],
                "promoted",
                event="promotion",
                reason="node promoted by reduction",
            )
        else:
            transition_node(
                state,
                node["id"],
                "pruned",
                event="prune",
                reason="direction closed by reduction",
            )

        return {
            "node_id": node["id"],
            "node_type": node["type"],
            "worker_status": worker_report["status"],
            "verdict": reduction["final_verdict"],
            "next_transition": reduction["next_transition"],
            "created_child_ids": [],
            "template_used": runner_summary["template_used"],
        }

    def _execute_runner(
        self,
        node: dict[str, Any],
        node_run_dir: Path,
    ) -> dict[str, Any]:
        experiment_plan, template_used = build_experiment_plan_for_node(
            self.repo_root, node, self.run_dir, settings=self.settings
        )
        validate_experiment_plan(node, experiment_plan, self.run_dir)
        experiment_plan_path = node_run_dir / "experiment_plan.json"
        _write_json(experiment_plan_path, experiment_plan)

        job_manifest = build_job_manifest_from_experiment_plan(
            node, experiment_plan, self.run_dir
        )
        validate_named_schema("job_manifest", job_manifest)
        job_manifest_path = node_run_dir / "job_manifest.json"
        _write_json(job_manifest_path, job_manifest)

        runner_result, multi_seed_summary = self._run_multi_seed(
            node, job_manifest, node_run_dir
        )
        validate_named_schema("runner_result", runner_result)
        runner_failure_memory = (
            record_runner_failure_candidate(self.repo_root, node, runner_result)
            if self.record_runner_failures
            else None
        )
        evidence_report = build_worker_report_from_runner_evidence(
            node, job_manifest, runner_result, self.run_dir
        )
        if multi_seed_summary is not None:
            # worker_report.schema.json forbids extra top-level fields, so
            # publish the multi-seed detail as a sidecar artifact and only
            # surface aggregated *_mean / *_std keys in metrics.
            sidecar = node_run_dir / "multi_seed_summary.json"
            _write_json(sidecar, multi_seed_summary)
            evidence_report.worker_report.setdefault("metrics", {}).update(
                multi_seed_summary["aggregated_metrics"]
            )
            try:
                artifact = str(sidecar.resolve().relative_to(self.run_dir.resolve()))
            except ValueError:
                artifact = str(sidecar.resolve())
            artifacts = evidence_report.worker_report.setdefault("artifacts", [])
            if artifact not in artifacts:
                artifacts.append(artifact)
        runner_result_path = Path(runner_result["workspace"]) / "runner_result.json"
        _attach_runner_outputs(
            node,
            self.run_dir,
            experiment_plan_path=experiment_plan_path,
            job_manifest_path=job_manifest_path,
            runner_result_path=runner_result_path,
            runner_result=runner_result,
            source_files=evidence_report.source_files,
            metrics_evidence_paths=evidence_report.metrics_evidence_paths,
        )
        node["outputs"]["template_used"] = template_used
        return {
            "worker_report": evidence_report.worker_report,
            "runner_failure_memory": runner_failure_memory,
            "template_used": template_used,
        }

    def _run_multi_seed(
        self,
        node: dict[str, Any],
        job_manifest: dict[str, Any],
        node_run_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Sakana multi_seed_eval parity.

        Runs `self.num_seeds` independent interpreter executions of the same
        manifest with the workspace re-seeded each run. Returns the *last*
        runner_result (for the existing evidence pipeline to consume) plus a
        summary capturing per-seed metrics, the cross-seed mean, and the
        cross-seed std for every numeric metric the runs emitted.

        When num_seeds == 1, returns (runner_result, None) — no overhead.
        """
        if self.num_seeds <= 1:
            return self.interpreter.run(job_manifest), None

        from research_harness.workers.workspace import ensure_path_inside

        per_seed: list[dict[str, Any]] = []
        last_runner_result: dict[str, Any] | None = None
        workspace = Path(job_manifest["workspace"]).resolve()
        for seed_index in range(self.num_seeds):
            seed_manifest = dict(job_manifest)
            seed_manifest["reproducibility"] = dict(job_manifest["reproducibility"])
            seed_manifest["reproducibility"]["seed"] = seed_index
            # Each seed needs its own scratch workspace so artifacts don't clobber.
            seed_workspace = workspace.parent / f"{workspace.name}_seed{seed_index:02d}"
            seed_workspace.mkdir(parents=True, exist_ok=True)
            ensure_path_inside(seed_workspace, self.run_dir, "multi_seed_workspace")
            seed_manifest["workspace"] = str(seed_workspace)
            # Re-materialize the source files under the seed workspace.
            for src in job_manifest.get("source_files", []):
                src_path = workspace / src
                dst_path = seed_workspace / src
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                dst_path.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")

            result = self.interpreter.run(seed_manifest)
            per_seed.append({"seed": seed_index, "runner_result": result})
            last_runner_result = result

        # Collect metrics from each seed's metrics.json files.
        per_seed_metrics: list[dict[str, float]] = []
        for entry in per_seed:
            ws = Path(entry["runner_result"]["workspace"])
            seed_metrics: dict[str, float] = {}
            for metrics_rel in job_manifest.get("outputs", {}).get("metrics_files", []):
                metrics_file = ws / metrics_rel
                if not metrics_file.is_file():
                    continue
                try:
                    payload = json.loads(metrics_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                raw = payload.get("metrics", payload)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            seed_metrics[str(k)] = float(v)
            per_seed_metrics.append(seed_metrics)

        aggregated = _aggregate_seed_metrics(per_seed_metrics)
        summary = {
            "num_seeds": self.num_seeds,
            "per_seed_metrics": per_seed_metrics,
            "aggregated_metrics": aggregated,
        }
        assert last_runner_result is not None
        return last_runner_result, summary

    def _template_summary(self, node: dict[str, Any]) -> tuple[str, bool]:
        domain = str(node.get("domain") or "").strip()
        if not domain:
            return ("No domain set on the node.", False)
        from research_harness.orchestrator.experiment_plan import (
            PLAN_METADATA_FILENAME,
            SRC_DIRNAME,
            template_directories,
        )

        for dirname in template_directories(self.settings):
            domain_dir = self.repo_root / dirname / domain
            plan_file = domain_dir / PLAN_METADATA_FILENAME
            src_dir = domain_dir / SRC_DIRNAME
            if plan_file.is_file() and src_dir.is_dir():
                try:
                    plan_meta = json.loads(plan_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    plan_meta = {}
                task_class = str(plan_meta.get("task_class", "unknown"))
                summary = (
                    f"domain={domain}, task_class={task_class}, dir={dirname}/{domain}/"
                )
                return (summary, True)
        return (
            f"experiment_plan_templates/{domain}/ not found; runner will fall back to demo plan.",
            False,
        )

    @staticmethod
    def _node_by_id(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        for node in state["nodes"]:
            if node["id"] == node_id:
                return node
        raise KeyError(node_id)

def _lightweight_worker_task(node: dict[str, Any]) -> dict[str, Any]:
    """Synthesize the minimum worker_task surface the grad student needs.

    We don't write this to disk — it's the in-memory brief the grad student
    LLM reads. The forbidden_actions list is the load-bearing field: even
    when the LLM produces commentary, it must never propose a child branch
    or change the claim contract.
    """
    return {
        "node_id": node["id"],
        "role": node["runtime_profile"]["worker_type"],
        "objective": "Run the assigned experiment and report observed evidence.",
        "scope_locks": dict(node["claim_contract"]),
        "forbidden_actions": [
            "change_claim_contract",
            "choose_or_replace_baselines",
            "mutate_shared_memory",
            "create_search_branch",
            "change_critic_routing",
            "change_publication_gate",
            "write_outside_workspace",
        ],
    }


def _aggregate_seed_metrics(per_seed: list[dict[str, float]]) -> dict[str, float]:
    """Cross-seed mean/std for every numeric metric key, suffixed with _mean / _std.

    Only keys that appear in at least one seed are included. Std is the
    population-std (divide by N) so a 1-seed run trivially reports std=0.
    """
    keys: set[str] = set()
    for seed_metrics in per_seed:
        keys.update(seed_metrics.keys())
    aggregated: dict[str, float] = {}
    for key in sorted(keys):
        values = [m[key] for m in per_seed if key in m]
        if not values:
            continue
        mean_val = sum(values) / len(values)
        var = sum((v - mean_val) ** 2 for v in values) / len(values)
        std_val = var ** 0.5
        aggregated[f"{key}_mean"] = round(mean_val, 6)
        aggregated[f"{key}_std"] = round(std_val, 6)
    return aggregated


def _worker_failure_source(worker_report: dict[str, Any]) -> str | None:
    artifacts = worker_report.get("artifacts") or []
    if not artifacts:
        return None
    return str(artifacts[0])


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
