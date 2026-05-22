from __future__ import annotations

from pathlib import Path
from typing import Any

from research_harness.memory.failure_retrieval import retrieve_failure_summaries


def build_failure_branch_prior(
    repo_root: Path,
    node: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Build non-binding branch priors from failure memory.

    These priors do not block promotion by themselves. They only tell the
    orchestrator what to control for if a child branch or retry is opened.
    """

    retrieval = node.get("failure_retrieval", {})
    query_tags = [node.get("domain", ""), *retrieval.get("query_tags", [])]
    top_k = int(settings.get("memory", {}).get("failure_retrieval_top_k", 5))
    summaries = retrieve_failure_summaries(
        repo_root,
        query_tags=[str(tag) for tag in query_tags],
        selected_fail_files=[str(item) for item in retrieval.get("selected_fail_files", [])],
        top_k=top_k,
    )
    risk_controls = [
        {
            "failure_file": summary.file,
            "category": summary.category,
            "tags": summary.tags,
            "lesson": summary.lesson,
            "required_control": f"Before retrying, address: {summary.reason}",
        }
        for summary in summaries
    ]
    return {
        "source": "failure_memory",
        "query_tags": query_tags,
        "selected_failure_files": [summary.file for summary in summaries],
        "risk_controls": risk_controls,
        "branch_suggestions": [
            {
                "type": "validity",
                "reason": control["required_control"],
                "source": f"failure_memory:{control['failure_file']}",
            }
            for control in risk_controls
        ],
    }
