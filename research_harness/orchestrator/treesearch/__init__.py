"""Sakana-v2-shaped tree search package.

Layout mirrors ai_scientist/treesearch/ from Sakana AI Scientist v2 — same
class names, same outer flow, but with claim-typed (not metric-typed) node
identity and the existing harness's claim_contract / critic / reduction
machinery underneath.

Public surface:
    Journal, Node           — claim-typed node collection (journal.py)
    Interpreter             — deterministic runner shim (interpreter.py)
    ParallelAgent           — per-stage concurrent stepper (parallel_agent.py)
    AgentManager, Stage     — outer 4-stage loop (agent_manager.py)
"""

from research_harness.orchestrator.treesearch.agent_manager import (
    AgentManager,
    Stage,
    StageTransition,
)
from research_harness.orchestrator.treesearch.interpreter import Interpreter
from research_harness.orchestrator.treesearch.journal import Journal, Node
from research_harness.orchestrator.treesearch.parallel_agent import ParallelAgent

__all__ = [
    "AgentManager",
    "Interpreter",
    "Journal",
    "Node",
    "ParallelAgent",
    "Stage",
    "StageTransition",
]
