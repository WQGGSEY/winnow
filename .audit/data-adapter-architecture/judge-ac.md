# Cross-judge: candidates A and C

## Verdict

Candidate C wins. It gives each ready dataset an immutable, content-addressed snapshot ID and carries that exact ID through connector selection, claim state, job binding, runner validation, and evidence. Candidate A freezes a thread-wide snapshot but lets claims select only the mutable adapter ID. Its private conversion of every local declaration to `type="custom"`, `role="primary"` is also incompatible with the existing dataset-spec taxonomy because `primary` is not a supported role.

| Criterion | Candidate A | Candidate C | Judgment |
|---|---:|---:|---|
| Correctness | 6/10 | 9/10 | C preserves declared materializer type and role, rejects `real_panel`, and keys materialization by the content snapshot. A invents an unsupported role and erases dataset semantics by mapping every local source to `custom`. |
| Active-path reachability | 8/10 | 10/10 | Both repair connector forest seeding and bind before `ready -> running`. C also specifies the fresh frontend acceptance path, requires deployment scope, and makes the selected snapshot part of every root. That closes the directional false-positive identified by the critics. |
| Interface depth | 8/10 | 9/10 | A's catalog hides substantial policy behind two methods, but the object has no durable state of its own and callers must coordinate its temporal methods. C separates immutable capability transforms from the one impure workspace-binding operation. Its three public transitions match the system's real lifecycle. |
| Simplicity | 7/10 | 9/10 | C uses two focused modules, no service lifecycle, one thread artifact, and one workspace artifact. A adds catalog, snapshot, binding, and receipt identities, then needs a closed union to reconcile them. |
| Idempotence | 8/10 | 9/10 | Both freeze thread state and atomically stage job inputs. C's per-adapter snapshot ID is the materializer ID, so unrelated adapter failures cannot perturb a selected dataset's identity. Rebinding verifies the same workspace artifact and rejects replacement. |
| Verification surface | 7/10 | 10/10 | C isolates parsing, precedence, probing, ID derivation, and selection as deterministic functions with an injectable observer and materializer. It also names the persisted evidence and the deployment-grade live check. A concentrates these cases inside a service and does not make the acceptance scope explicit. |
| **Total** | **44/60** | **56/60** | **Use C as the base.** |

## Strongest graft from candidate A

Graft A's invalid-operator-override rule into C's parser and merge contract. If an operator row targets an existing project adapter ID but is malformed or unready, that ID must become unavailable. The resolver must not discard the bad operator row and silently expose the project entry underneath it.

C's current `parse_adapter_entries()` returns valid declarations separately from problems, then `merge_adapter_entries()` sees only valid declarations. That shape cannot distinguish "no operator override" from "operator attempted an invalid override". Preserve an operator tombstone or keyed resolution problem through the merge. The final per-ID result should be `Ready(AdapterSnapshot)` or `Unavailable(AdapterProblem)`, with operator scope taking precedence in both cases. This keeps one precedence policy for the frontend and supervisor and honors operator intent without fallback surprises.

## Fatal red flags

### Candidate A

The `role="primary"` conversion is fatal as written. The existing dataset-spec role enum accepts `training`, `evaluation`, `baseline_reference`, `baseline_implementation`, and `other`. A would either fail at the materializer boundary or require a hidden schema exception. Mapping every local declaration to `type="custom"` also recreates the taxonomy ambiguity called out by the abstraction critique. Do not use A as the base without replacing that model.

A also hashes rejected records into one thread snapshot ID. An unrelated unavailable adapter can therefore change the identity used to describe a ready adapter set. This is not fatal for the local acceptance run, but it is inferior to C's per-adapter content identity.

### Candidate C

C has no fatal whole-design flaw, but its precedence ambiguity must be fixed before implementation. Its filesystem claim must also stay honest. Workspace-relative paths and digest checks prove what the runner delivered; they do not contain an unsandboxed child process. The acceptance assertion must inspect persisted runner input evidence and must not claim exclusive dataset consumption.

## Pick and graft

Use candidate C's functional snapshot pipeline as the base. Add A's fail-closed operator override semantics by carrying keyed unavailable results through precedence resolution. Reject A's stateful catalog, universal `custom` conversion, unsupported `primary` role, and extra binding identity. Then verify one deployment-scope arXiv run where the forest root contains the selected snapshot ID, plan and job inputs reference the workspace manifest, and `runner_result` echoes the same snapshot and content digest.
