# Architecture selection rubric

Each candidate receives a score from 0 to 3 for each criterion.

1. The immutable goal owns the shared baselines, success criteria, and disproof
   conditions. A strategy cannot replace its own bar. Existing integrity gates
   remain intact.
2. A negative observation changes explicit beliefs and can produce structurally
   distinct strategy candidates. The design rejects repeated strategies and
   evidence-equivalent experiments before execution.
3. The harness derives selection priority from observable components. The LLM
   can propose mechanisms and predictions but cannot stamp an opaque winning
   score.
4. Budget and capability exhaustion create an idempotent, resumable pause. Only
   a strategy that clears the frozen bar can complete the research program.
5. The active MCP path has one policy owner. The migration removes or isolates
   duplicate legacy policy rather than adding another controller.
6. The first vertical slice can be verified against the recorded arXiv trace and
   an active-path integration test without implementing the whole future system.
