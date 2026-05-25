"""Claim-typed Journal — Sakana parity in shape, claim-first in semantics.

Sakana's Journal keys identity off Node.metric (a numeric score). Ours keys
identity off (node_type, claim_contract) and the verdict label produced by
reduce_node. The class names and public surface match Sakana so the outer
AgentManager can navigate it the same way, but `good_nodes` means
"promoted under claim_contract" — not "best metric so far".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from research_harness.orchestrator.search_state import (
    TERMINAL_STATUSES,
    validate_search_state,
)


PROMOTED_VERDICTS = {"supported_with_scope_narrowing", "supported"}


@dataclass
class Node:
    """View onto a single node + its reduction outcome.

    Wraps the underlying search_state node dict so existing schema validation
    remains the source of truth. Adds Sakana-shaped convenience accessors
    that the AgentManager uses (parent, children, is_buggy, verdict).
    """

    raw: dict[str, Any]
    reduction: dict[str, Any] | None = None
    worker_report: dict[str, Any] | None = None
    depth: int = 0

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def node_type(self) -> str:
        return str(self.raw["type"])

    @property
    def stage(self) -> str:
        return str(self.raw["stage"])

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def parent(self) -> str | None:
        return self.raw.get("parent")

    @property
    def claim_under_test(self) -> str:
        return str(self.raw["claim_contract"]["claim_under_test"])

    @property
    def verdict(self) -> str | None:
        if self.reduction is None:
            return None
        return str(self.reduction.get("final_verdict") or "")

    @property
    def is_buggy(self) -> bool:
        """Sakana parity: did this node fail to evaluate the claim?

        True iff the worker report is missing/non-completed OR the reduction
        labelled it confounded/not_evaluable/contradicted.
        """
        if self.worker_report is not None and self.worker_report.get("status") != "completed":
            return True
        if self.verdict in {"confounded_or_not_evaluable", "contradicted"}:
            return True
        return False

    @property
    def is_promoted(self) -> bool:
        return self.status == "promoted" or self.verdict in PROMOTED_VERDICTS


@dataclass
class Journal:
    """Collection of Node views over a single search_state.

    Sakana parity:
        len(journal), journal[i], journal.append, journal.draft_nodes,
        journal.buggy_nodes, journal.good_nodes, get_node_by_id, get_best_node.

    Differences from Sakana:
        - get_best_node returns the most recently promoted node under the
          claim_contract — there is no metric ranking.
        - draft_nodes are root_node_from_grilling outputs (parent is None).
    """

    search_state: dict[str, Any] = field(default_factory=dict)
    reductions: dict[str, dict[str, Any]] = field(default_factory=dict)
    worker_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    promotion_order: list[str] = field(default_factory=list)

    @classmethod
    def from_search_state(cls, search_state: dict[str, Any]) -> "Journal":
        validate_search_state(search_state)
        return cls(search_state=search_state)

    @property
    def nodes(self) -> list[Node]:
        return [self._view(raw) for raw in self.search_state.get("nodes", [])]

    def __len__(self) -> int:
        return len(self.search_state.get("nodes", []))

    def __getitem__(self, idx: int) -> Node:
        return self._view(self.search_state["nodes"][idx])

    def append(self, raw_node: dict[str, Any]) -> None:
        """No-op: appends are owned by orchestrator.search_state.add_child_nodes.

        Journal is a *view*; mutating the underlying search_state goes through
        the existing transition_node / add_child_nodes API so we don't bypass
        schema validation. This method exists for Sakana parity only.
        """
        raise NotImplementedError(
            "Journal.append is a Sakana parity stub. Use "
            "orchestrator.search_state.add_child_nodes to extend the tree."
        )

    @property
    def draft_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.parent is None]

    @property
    def buggy_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.is_buggy]

    @property
    def good_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.is_promoted]

    @property
    def terminal_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.status in TERMINAL_STATUSES]

    def get_node_by_id(self, node_id: str) -> Node | None:
        for raw in self.search_state.get("nodes", []):
            if raw["id"] == node_id:
                return self._view(raw)
        return None

    def get_best_node(self, only_good: bool = True) -> Node | None:
        """Claim-first replacement for Sakana's metric-ranked best.

        Returns the most recently promoted node (last in promotion_order),
        or None if none have promoted. With only_good=False, falls back to
        the most recently reduced node.
        """
        if self.promotion_order:
            return self.get_node_by_id(self.promotion_order[-1])
        if only_good:
            return None
        for raw in reversed(self.search_state.get("nodes", [])):
            if raw["id"] in self.reductions:
                return self._view(raw)
        return None

    def record_reduction(
        self,
        node_id: str,
        worker_report: dict[str, Any],
        reduction: dict[str, Any],
    ) -> None:
        """ParallelAgent calls this after each per-node pipeline step."""
        self.worker_reports[node_id] = worker_report
        self.reductions[node_id] = reduction
        if reduction.get("next_transition") == "promoted":
            if node_id not in self.promotion_order:
                self.promotion_order.append(node_id)

    def coverage_by_node_type(self) -> dict[str, list[str]]:
        """How many nodes of each type have been promoted.

        Used by AgentManager.Stage.exit_predicate — stages advance based on
        claim-type coverage, not on a metric improving.
        """
        coverage: dict[str, list[str]] = {}
        for node in self.nodes:
            if not node.is_promoted:
                continue
            coverage.setdefault(node.node_type, []).append(node.id)
        return coverage

    def _view(self, raw: dict[str, Any]) -> Node:
        node_id = raw["id"]
        depth = 0
        for item in self.search_state.get("frontier", []):
            if item["node_id"] == node_id:
                depth = int(item["depth"])
                break
        return Node(
            raw=raw,
            reduction=self.reductions.get(node_id),
            worker_report=self.worker_reports.get(node_id),
            depth=depth,
        )
