# Cross-judge: candidate B versus candidate C

## Verdict

**Winner: Candidate C.** It is the stronger base because it preserves the required lifecycle split: thread bootstrap observes and snapshots readiness; node execution binds that exact immutable snapshot through the existing materializer before `ready -> running`. Its snapshot ID flows through the envelope, connector roots, plan/job input reference, workspace manifest, and runner evidence. That gives one traceable execution identity without treating mutable adapter ID as byte identity.

Candidate B has the better operator-selection entry point, but its core shape is broader and less faithful to the required two-stage seam. It turns the legacy `DatasetManifest` into a thread-wide generalized catalog for local, Hugging Face, and synthetic data, and materializes every ready dataset during bootstrap. Job-time “binding” then copies a manifest that still points at absolute cache paths rather than materializing and staging the selected snapshot at the job boundary.

## Scores

Scores are out of 10.

| Criterion | Candidate B | Candidate C | Judgment |
|---|---:|---:|---|
| Correctness | 7 | 9 | Both separate registration from readiness and add content identity. C additionally carries an immutable snapshot ID on the claim and verifies the selected bytes after materialization. B relies on a thread manifest plus mutable dataset ID and changes acquisition timing. |
| Active-path reachability | 9 | 8 | Both repair connector forest seeding and bind before execution. B’s `watch --data-source-anchor` plus envelope-owned selection is directly operator-controlled and automatically available to seeding. C requires `selected_snapshot_id` to arrive in the MCP seed call, leaving one more model-mediated handoff. |
| Interface depth | 7 | 8 | B hides much behind one manifest module but makes that artifact serve registry, readiness, acquisition, selection, and delivery. C keeps two artifacts with distinct authorities and exposes three meaningful operations: snapshot, select, bind. C’s lower-level parse/observe helpers should remain internal where callers do not need them. |
| Simplicity | 5 | 8 | B introduces a full `DatasetSpec` union, remote/synthetic readiness, a version-2 manifest, and worker-report propagation beyond the local acceptance need. C limits the adapter contract to registered local sources and leaves unrelated acquisition paths alone. |
| Idempotence | 8 | 9 | Both use content identity and atomic persistence. C uses snapshot ID as the existing materializer’s dataset ID, avoiding stale cache reuse without changing `LocalPathMaterializer`; it also refuses to overwrite a workspace bound to different evidence. |
| Verification surface | 7 | 9 | B provides fingerprint and runner validators, but its large manifest builder couples settings, remote acquisition, hashing, and persistence. C isolates pure parsing, precedence, observation-to-snapshot, selection, spec adaptation, and an injected materializer call, allowing narrow deterministic tests at every boundary. |

**Total: B 43/60; C 51/60.**

## Strongest graft from candidate B

Graft B’s **operator-owned selection at supervisor bootstrap** into C. The frontend/supervisor should accept a human adapter ID (or accept the sole ready adapter), persist the chosen `{adapter_id, snapshot_id}` in `feasibility_envelope.operator_intent`, and let connector seeding require that exact selection. This is stronger than asking the MCP caller to supply an unconstrained `selected_snapshot_id` later: it makes normal-path reachability deterministic, keeps selection out of Professor-authored state, and lets forest seeding validate rather than decide.

The graft should retain C’s immutable execution identity. The operator chooses by stable human ID; bootstrap resolves that ID to exactly one ready snapshot; the envelope persists both; every connector root receives both; job binding uses the snapshot ID.

## Fatal red flags

### Candidate B

1. **Bootstrap performs acquisition, collapsing the two stages.** `ensure_thread_dataset_manifest()` materializes all registered datasets before the feasibility envelope. That makes thread readiness potentially download or synthesize data and leaves job execution with only manifest copying. The requested seam is probe/snapshot at thread lifetime and selected bind/materialize at job lifetime.

2. **The design is a generalized data catalog beyond the demonstrated need.** Its public model includes local, Hugging Face, and synthetic specs, selection, unavailable states, a versioned manifest migration, and worker evidence propagation. This violates the grounding constraint that the smallest useful local-adapter shape wins and substantially enlarges the failure and test surface.

3. **The workspace contract still exposes host cache layout.** The staged manifest retains absolute `materialized_path` values and experiment code opens them directly. The manifest file is workspace-local, but delivery remains coupled to a mutable external cache path and does not gain C’s workspace-staged byte boundary.

### Candidate C

No fatal red flag. Two issues need tightening during synthesis:

- Make operator selection part of bootstrap/envelope state using the B graft; do not leave resource authority to a later MCP argument.
- Keep `parse_adapter_entries`, `merge_adapter_entries`, `observe_local_source`, and `probe_adapter` private unless the frontend genuinely needs them. The production interface should remain snapshot, select, and bind rather than expose temporal implementation steps.

## Verification against the grounding constraints

With the selection graft, C reaches the active frontend path end to end: one resolver reads project and operator declarations; only probed snapshots enter the envelope; connector seeding stamps scope, adapter ID, and snapshot ID on every root; execution binds before `running`; the harness overwrites model inputs with a typed workspace-manifest reference; and `LocalRunner` validates and echoes delivery evidence. It reuses `DatasetMaterializer`, is retry-stable through content-addressed IDs and atomic writes, and adds no dynamic loader or second fetcher framework.
