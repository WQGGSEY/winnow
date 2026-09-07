# ADR 0016: Exploration must feed constructive research

- Status: Accepted
- Date: 2026-09-06
- Amends: ADR 0015's universal competing-prediction requirement
- Preserves: immutable GoalContract, one active DirectionAttempt, strong-result terminal

## Decision

ResearchWork separates inquiry mode from execution kind. Exploration may record
phenomena before a competing explanation exists, so it may have no alternatives.
Discrimination and intervention require competing predictions. An informative
exploratory result need not confirm an existing prediction. Measurement validity
and eligible-observation requirements still apply; informative does not mean a
validated claim.

Each plan carries a provisional solution_path: current explanation, unexplained
observations, changed assumption, possible intervention, expected effect on the
original goal, next solution decision and original-scope check. Its parent work
ID must link the current solution record. The working brief retains the lineage
and supplies its recent versions to planning and hypothesis development. Unknown
interventions are allowed during exploration; the planner must explain what
observation would enable their design.

A diagnosis is useful intermediate knowledge, never completion of a solution
goal. The planner is instructed to reconsider assumptions, representations or
interventions when repeated diagnosis does not change a solution decision. A
cheap plausible intervention does not require a complete causal theory first.
Privileged diagnostic inputs and simplified conditions cannot qualify the
original task. Strong-result and publication gates remain authoritative.

## Limits

The host verifies mode and ancestry, not the semantic quality of explanations or
interventions. Stagnation handling and reserve allocation remain planner
judgments, not a new proven optimizer. There is no automatic numeric confidence
promotion, extra always-on review panel, or root-authored Pacman learner.

For bounded live evaluation, supervisor model selection also configures internal
planning, hypothesis and review calls. Sol maps to low and Luna to max. An
optional shared call budget limits adapter process launches, submitted initial
prompt bytes and call admission deadline. This is not an exact token/cost cap:
CLI tool-result context and in-flight output are not covered by prompt bytes.
The live runner also needs an external wall-clock stop. Budget exhaustion is not
scientific completion.

The initial bounded run exposed a separate bypass: native CLI subagents were
spawned through an inherited host research skill, outside adapter reservations.
Bounded calls now disable native multi-agent tools and host skill discovery.
The retry reuses the original budget and deadline. The MCP subprocess receives
the budget path explicitly; inheriting it only in the supervisor environment
does not propagate it through the CLI MCP boundary. The quota alone is not an
execution boundary. ResearchWork preparation now uses managed editing and reading
tools, and research_review analysis/review calls use a thread-scoped read-only MCP.
They cannot launch native shell commands; fitting and simulation belong to the
registered runner. The reader also permits framework source inspection, rejects
mutation tools even when called directly, and excludes the review's own private
request and event files. This does not turn initial prompt bytes into a token cap.

Interrupted reviews retain successful reads, including MCP reads. Large source or
data bundles remain at hash-bound materialized paths instead of being inlined into
an already bounded prompt. Such a recovery retains the reader for missing evidence;
small fully supplied revisions may still use tool-free synthesis. Unregistered
execution attempts must be disclosed to scope review as well as source review.
Their actual stage and the operative protocol determine whether another execution
is allowed; a transport failure is neither automatic permission nor a scientific
negative.

## Bounded-run evidence

The first live check failed to reach execution. A solution_path was emitted but
contained a stale failure explanation and an invalid prediction update. See
`docs/research/luna-bounded-insight-run.md` for the cost and stop record.

Policy 20 additionally moves large secondary planning fields to content-hashed
files for targeted reads and separates the authoritative latest outcome from
old intent prose. This post-run correction has only offline validation. File
references do not waive protocol requirements or establish research progress.
