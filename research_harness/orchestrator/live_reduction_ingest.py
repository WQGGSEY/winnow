from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from research_harness.config import load_settings
from research_harness.critics.governance import select_critics
from research_harness.critics.review_runner import run_critic_reviews
from research_harness.orchestrator.branch_prior import build_failure_branch_prior
from research_harness.orchestrator.reduction import reduce_node
from research_harness.orchestrator.search_state import validate_search_state
from research_harness.schemas.validator import validate_named_schema


class LiveReductionIngestError(ValueError):
    """Raised when a live dispatch cannot be reduced safely."""


def build_live_reduction_bundle(
    repo_root: Path,
    dispatch_path: Path,
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Turn a live worker report into critic/reduction artifacts.

    This function deliberately does not mutate search_state or shared memory.
    It creates the review bundle an operator can inspect before applying the
    reduction to the search frontier in a separate approval step.
    """

    repo_root = repo_root.resolve()
    dispatch_path = dispatch_path.resolve()
    dispatch = _load_json(dispatch_path)
    validate_named_schema("live_node_dispatch", dispatch)
    if dispatch["mode"] != "execute_once":
        raise LiveReductionIngestError("live reduction ingest requires execute_once dispatch")
    if not dispatch.get("live_summary_path"):
        raise LiveReductionIngestError("live dispatch does not reference a live summary")

    search_state_path = Path(dispatch["search_state_path"]).resolve()
    search_state_digest = _sha256_file(search_state_path)
    search_state = _load_json(search_state_path)
    validate_search_state(search_state)

    node = _node_by_id(search_state, dispatch["node_id"])
    if node["status"] != "ready":
        raise LiveReductionIngestError(
            f"live reduction ingest requires a ready node, got {node['status']}"
        )

    live_summary_path = Path(dispatch["live_summary_path"]).resolve()
    live_summary = _load_json(live_summary_path)
    validate_named_schema("live_smoke_run_summary", live_summary)
    if live_summary["node_id"] != node["id"]:
        raise LiveReductionIngestError("live summary node_id does not match dispatch node")
    if not live_summary.get("worker_report_path"):
        raise LiveReductionIngestError("live summary does not reference a worker report")

    worker_report_path = Path(live_summary["worker_report_path"]).resolve()
    worker_report = _load_json(worker_report_path)
    validate_named_schema("worker_report", worker_report)
    if worker_report["node_id"] != node["id"]:
        raise LiveReductionIngestError("worker report node_id does not match dispatch node")

    critic_routing = select_critics(repo_root, node)
    critic_reviews = run_critic_reviews(node, worker_report, critic_routing)
    for review in critic_reviews:
        validate_named_schema("critic_review", review)

    settings = load_settings(repo_root)
    failure_branch_prior = build_failure_branch_prior(repo_root, node, settings)
    orchestrator_reduction = reduce_node(
        node,
        worker_report,
        critic_reviews,
        branch_prior=failure_branch_prior,
    )

    live_run_dir = Path(dispatch["live_run_dir"]).resolve()
    critic_review_bundle_path = live_run_dir / "live_critic_review_bundle.json"
    orchestrator_reduction_path = live_run_dir / "live_orchestrator_reduction.json"
    bundle_path = (output_path or live_run_dir / "live_reduction_bundle.json").resolve()

    critic_review_bundle = {
        "routing": critic_routing,
        "reviews": critic_reviews,
    }
    _write_json(critic_review_bundle_path, critic_review_bundle)
    _write_json(orchestrator_reduction_path, orchestrator_reduction)

    bundle = {
        "type": "live_reduction_bundle",
        "status": "ready_for_apply",
        "node_id": node["id"],
        "search_state_path": str(search_state_path),
        "search_state_sha256": search_state_digest,
        "dispatch_path": str(dispatch_path),
        "live_summary_path": str(live_summary_path),
        "worker_report_path": str(worker_report_path),
        "critic_review_bundle_path": str(critic_review_bundle_path),
        "orchestrator_reduction_path": str(orchestrator_reduction_path),
        "bundle_path": str(bundle_path),
        "state_mutation": "forbidden",
        "memory_mutation": "forbidden",
        "apply_required": True,
        "node": node,
        "worker_report": worker_report,
        "critic_routing": critic_routing,
        "critic_reviews": critic_reviews,
        "failure_branch_prior": failure_branch_prior,
        "orchestrator_reduction": orchestrator_reduction,
        "error": None,
    }
    validate_named_schema("live_reduction_bundle", bundle)
    _write_json(bundle_path, bundle)
    return bundle


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LiveReductionIngestError(f"expected JSON object: {path}")
    return data


def _node_by_id(search_state: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in search_state["nodes"]:
        if node["id"] == node_id:
            validate_named_schema("node", node)
            return node
    raise LiveReductionIngestError(f"node not found in search_state: {node_id}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build critic/reduction artifacts from one live dispatch result."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--dispatch", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        bundle = build_live_reduction_bundle(
            args.repo_root,
            args.dispatch,
            output_path=args.output,
        )
    except LiveReductionIngestError as exc:
        print(
            json.dumps(
                {
                    "type": "live_reduction_ingest_error",
                    "status": "blocked",
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(bundle, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
