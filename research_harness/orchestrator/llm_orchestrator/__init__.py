"""LLM-orchestrated tree search components.

Adds a Professor and a GradStudent role on top of the Sakana mirror so the
outer loop is driven by reasoning rather than by a deterministic vending
machine. Workers still cannot expand scope, mutate claims, write shared
memory, or create branches — the invariants stay intact.
"""

from research_harness.orchestrator.llm_orchestrator.dialog import (
    DialogEntry,
    DialogLog,
)
from research_harness.orchestrator.llm_orchestrator.grad_student import (
    GradStudent,
    GradStudentConcern,
)
from research_harness.orchestrator.llm_orchestrator.llm_client import (
    ClaudeCliLLMClient,
    LLMClient,
    LLMResponse,
    MockLLMClient,
    build_llm_client,
)
from research_harness.orchestrator.llm_orchestrator.professor import (
    Professor,
    ProfessorDecision,
)

__all__ = [
    "ClaudeCliLLMClient",
    "DialogEntry",
    "DialogLog",
    "GradStudent",
    "GradStudentConcern",
    "LLMClient",
    "LLMResponse",
    "MockLLMClient",
    "Professor",
    "ProfessorDecision",
    "build_llm_client",
]
