# ADR 0018: Observation contracts and partial interpretation

Status: implemented, live research effect unverified

## Problem

The Pacman diagnostic completed but its selected count names were absent from the
reported top-level metrics. Family counts existed inside `details`. Implementation
review approved the program without checking the selected output names. The next
planner received a large history, while a global inconclusive result forced every
prediction to remain unresolved. Repeated operational recovery could also prevent
a different diagnostic or a small exploratory intervention.

## Decision

An empirical ResearchWork declares count names in `required_observations`. Each
alternative declares the subset it needs, including shared validity/support counts.
The execution agent supplies `experiment_plan.observation_bindings`: exact declared
metrics artifact, JSON pointer, and source producer for every count. The host
rejects missing/extra bindings or out-of-scope artifact paths before independent
review. Both template-building routes preserve these bindings.

The reviewer must return one `observation_checks` item per binding. Approval needs
an affirmative producer-to-output check for each count. This tests emission, not
whether the measured count or treatment effect will be positive. The host still
resolves the actual values after execution and records their paths and file hashes.
It never guesses aliases or silently sums groups. Semantic fidelity of the producer
remains a reviewer responsibility, not a static proof supplied by the host.

A completed work may support a `partial` interpretation. An updated empirical
prediction must cite all of its own prospectively declared observation dependencies,
with finite positive support counts. Missing or empty dependencies force only that
prediction to remain unresolved. The generated response schema constrains these
choices before generation; host validation repeats the check afterward. Actual
execution failure still leaves all predictions unresolved. Positive support does
not prove an effect, validity, power or generalization. Publication and confirmation
gates are unchanged.

Historical work keeps its original interpretation boundary. Missing per-prediction
maps fall back conservatively to the previous whole-work support requirements. We
do not retroactively repartition a failed work to manufacture a favorable result.
Existing-source analysis may examine already recorded values and limits without
replaying the experiment. Future work must declare its own scoped contract.

## Planning

The selector receives a deterministic `decision_focus`, per-prediction support,
and shallow scalar facts with exact artifact JSON pointers. Deeper structures remain
addressable; they are not removed or treated as absent. Relevant recent analyses
remain inline and older analysis content is archived. This is a bounded projection,
not an extra paid interpretation agent or an LLM-authored scientific summary.

Operational failure does not refute a hypothesis, but may justify replacing the
procedure on cost, feasibility or discriminating value. Existing-source recovery
is preferred when needed quantities are already recorded. Small exploratory
competence/comparison controls may follow unusable observations. The planner again
explicitly permits plausible interventions before a complete causal explanation,
requires a matched control and an original-goal effect, and asks repeated diagnosis
to change a solution decision. These semantic choices remain model judgments.

Planning policy is 23; old planned work must be reconsidered. Implementation-review
policy is 12 and review transport version is 4. No current research result or learner
was edited to accommodate the new contracts.

## Evidence and limits

See `docs/research/research-cycle-thought-experiment.md`. Offline replay exercised
bindings, partial-interpretation boundaries and the actual latest result projection.
It did not run Luna, a new experiment or a paper-generation cycle. Success remains
a hands-off strong-result paper, not schema conformance.
