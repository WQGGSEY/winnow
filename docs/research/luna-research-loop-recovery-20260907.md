# Pacman research-loop recovery, 2026-09-07

Scope: priorities 1–4, ending in a harness-authored intervention and an actual
controlled comparison. Strong-result confirmation and manuscript production are
subsequent work, not demonstrated by a diagnostic or passing software checks.

## Planner isolation

`runs/e2e/lab3-game-only/planner-time-isolation` preserves the original prompt,
packet and output schema, the live Luna max response, usage and host rejection.
Only the per-call timeout changed from 240 to 600 seconds. The call completed in
140.90 seconds with 43,498 input tokens and 7,494 output tokens, including 6,285
reasoning tokens. The prior run timed out at 240 seconds without terminal usage.
This single comparison does not establish that a higher timeout caused completion:
the completed response arrived inside the former limit. Latency variance and task
burden remain plausible contributors; timeout alone is not the demonstrated root
cause of all failures.

The completed response selected existing-source analysis of missing observation
bindings. Its scientific text was produced by Luna. The host rejected the entire
decision because `solution_path.parent_work_id` was null even though the preceding
work was already known. This was an unnecessary generation responsibility.

The response schema now leaves lineage assignment to the host. Canonical stored
work still requires and validates its exact parent. Raw model output is retained.
The saved real response was passed through the normal planner validation again,
with zero new model calls. The recovery asserted that research inputs were equal
apart from rejection feedback and that every scientific decision field was
unchanged. Only host-owned lineage changed. `recovered_result.json` records the
source response hash and accepted work
`126b48797f1c568c3b1ab9426e2a7451a8d42d9e636abb5b01550296697659d0`.

## Analysis responsibility

The analysis role now receives selected evidence and recorded JSON measurement
facts first. Other history remains in hash-addressed files for targeted inspection.
The full packet is retained for audit and validation. This does not qualify the
old diagnostic or reinterpret zero eligible groups as a scientific negative.

For the resumed work the full analysis packet occupied 396,749 bytes; the live
initial prompt, including instructions, occupied 29,493 bytes. The supervisor run
is `runs/e2e/lab3-game-only/luna-cycle-e2e-20260907-055131`. Its limits are 20 minutes,
10 calls, 1 MB aggregate initial prompts and 280 KB per call. Prompt-byte limits
are not billed-token limits.

## Outcome boundary

The recovered work has entered live source analysis. An intervention and controlled
comparison have not yet been established at this checkpoint. The root agent has
not supplied an RL learner or scientific intervention. 37 focused control/review
checks passed; these protect implementation behavior and do not demonstrate
research success.

## Interrupted inspection recovery

The focused analysis reached the metric producer and read the selected artifacts,
but timed out at 300 seconds before returning a final answer. Its event stream
contained 12 successful read-only command outputs, including the full metric
producer and computed summaries of the existing artifact. No accepted analysis
was persisted. Input compaction alone was therefore insufficient.

Analysis recovery now separates these completed observations from unfinished
interpretation. It retains successful tool outputs verbatim, their output hashes,
commands and original event-stream hash. Failed commands and incomplete event
lines do not become evidence. Duplicate outputs are removed and an 80 KB bound
on the serialized observations reports any omissions. All 12 outputs fit for this
run, with zero omissions. A resumed synthesis call gets those observations and
the selected question with local tools disabled. It must answer from that record
or explicitly identify missing evidence; it cannot buy another round of the same
inspection. The original events remain immutable and the synthesis cache key
includes the recovered observations.

The `inspection-recovery` continuation retains the original run's deadline and
call ledger. All processes belonging to the failed budget were absent before
resumption. The live synthesis initial prompt is 105,277 bytes. A focused
regression also exercises an interrupted trailing JSON event, exact observation
retention, disabled synthesis tools and reuse of the accepted analysis receipt.
38 control/review checks passed. The scientific result is still pending.

The recovered synthesis completed and was persisted as `analysis_completed`. It
reported recoverable counts, a fixed-grid feed-forward no-signal result, and an
unevaluable recurrent result because all groups represented single primitive
states. Its usage was 50,752 input and 7,439 output tokens, with 6,732 reasoning
tokens included in output. The next planner used this finding without rerunning
the aliasing experiment.

## Planner completion and citation contract

The next 240-second planner timed out. In run
`luna-cycle-e2e-20260907-060835`, the same saved research input and output contract
were submitted with a 600-second limit. Input equality was checked. It completed
in approximately 333 seconds, using 44,751 input and 18,218 output tokens, of
which 16,563 were reasoning tokens. This completed call required more than the
old limit. The earlier 140.90-second completion also shows substantial variability.
The production planner limit is now 600 seconds, still bounded by the run's
remaining global deadline; this avoids imposing the known inadequate 240-second
cap on every expensive reasoning attempt.

The model selected a matched task-feasibility/reward diagnostic rather than
repairing the recurrent denominator again. However, it put raw artifact pointers
into `prediction_updates.observation_ids` while interpreting source analysis.
That field accepts declared observation names; analysis declares none. The host
correctly rejected it but the generated schema had failed to constrain the field
when no measurement-support record existed. The schema now binds observation
citations for every previous prediction, including empty citation arrays for
source analysis. Existing source/analysis receipt citations remain available.

The focused reproduction also found that the local JSON-schema subset ignored
`maxItems`. It now enforces array upper bounds, so local validation respects the
same empty-array contract. 38 research-control/review checks and 23 existing
schema checks passed. These checks do not substitute for a completed experiment.

Supervisor guidance also still said repeated identical observations should always
lead to causal discrimination. It now follows the planner's exploration,
discrimination or intervention choice and allows replacing diagnostics that do
not change the next intervention. The original goal, matched comparisons and
unverified status of intuition remain explicit.

## Execution handoff and historical evidence loss

The corrected planner registered work
`95de6a19a52f95f4d1b06ba80f698c46a7dd596f7031491f3620aa16a08c373a`.
Its first dispatch had no experiment_plan because a new unimplemented work pointed
straight to execution. New empirical work now points to source preparation;
preparation preserves the same scientific work and then points to preflight.
The live agent subsequently authored its control program and explicit count
bindings. The root did not author the experiment.

The first implementation review timed out at 300 seconds after ten successful
read-only outputs. Recovery now supports execution reviews as well as analysis.
The coordinator independently found a real source discrepancy during resumption:
`testCapture` remained in the copied source where the selected work specified
`bloxCapture`. It changed that line. Consequently the input digest changed and
old inspection was correctly not reused for the changed source. The new source
entered a fresh independent review. This is a code correction, not evidence of
an RL intervention or a successful control result.

A more consequential memory defect was also found. `development_evidence` admitted
only the newest eight reports, and the selector then saw only the latest two.
The earlier completed collect-and-return feasibility control was absent both
from its visible results and its available evidence IDs. The selector could not
know that this related question had already been measured. The earlier control
and the new matched reward-alignment diagnostic are not asserted to be equivalent;
the omission prevented an informed comparison of their scope in the first place.

A historical result index now retains all completed development measurements
with declared objectives, observed metrics, receipt paths and artifact digests.
For this thread it contains 69 measurements in approximately 44 KB, rather than
full source and tool histories. These entries are discoverable and citable by the
selector and selected source-analysis role. The index explicitly requires checking
protocol and implementation differences before reuse. It does not qualify any
result or expose final-falsifier files. Recent execution reconciliation still uses
its existing bounded window.

The historical-window regression retains the oldest of ten measurements and
leaves a final-falsifier sentinel unread. The combined control/review checks pass
40 cases. A transport checkpoint handoff now gives unchanged saved-request
arguments, distinguishing retry from an implementation objection and discouraging
coordinator rereads of reviewer logs.

## Correcting inspection retention

The changed-source review exhausted the remaining global deadline after roughly
230 seconds. A fresh bounded run, `luna-cycle-e2e-20260907-063918`, attempted
synthesis from that exact request. Inspection of the recovery revealed a defect
in the new retention logic: retaining the most recent 80 KB favored later harness
implementation reads and omitted the experiment source read. The root interrupted
that synthesis before any assessment was accepted and recorded a transport
checkpoint. This interrupted call's billed usage is unknown.

Recovery now prioritizes commands reading the bound experiment source, followed
by its workspace inputs, ahead of unrelated framework reads. The full hash-checked
proposed source is also supplied inline to a tool-free execution reviewer. Output
omissions remain explicit; missing evidence does not grant approval. A targeted
case confirms that a 4 KB source read survives a later 77 KB framework read.
The corrected continuation `source-retention-recovery` retains the same run
ledger/deadline. Its initial review prompt is 262,965 bytes, below the 280 KB
per-call ceiling. 41 control/review checks passed. Actual review and experiment
outcomes are still pending at this checkpoint.

## Separating protocol scope from code judgment

The unchanged source-preserved synthesis also timed out at 600 seconds. No final
assessment or usage record was returned. Increasing the timeout alone did not
resolve that combined review. The retained protocol history contained 30 distinct
approved amendments, about 107 KB; removing exact duplicate text was not a solution.

Execution review now first records a separate, tool-free protocol-scope analysis
of the selected question and supplied registered text. It does not inspect or
approve code. The code reviewer receives that fallible guide, the original
history reference, and the actual source/inspection evidence. The original packet
remains available for exact blocking-clause validation. No protocol is rewritten
or waived. The guide is cached across source revisions of the same work; a changed
protocol or selected question changes its input digest.

Run `luna-cycle-e2e-20260907-070544` reached a real scope answer in about 321 seconds
from 124,260 initial prompt bytes. Usage: 49,251 input and 17,558 output tokens,
including 16,836 reasoning tokens. Status was unresolved: historical permission
bound a single collect-and-return task-feasibility execution on tiny/alley/test,
while a later test-to-blox replacement applied explicitly to training/Q2 grids.
It did not clearly authorize the new two-arm naive/random tiny/alley/blox test.
The analysis identified the relevant old and replacement clauses rather than
inventing an implementation defect or approving the ambiguity.

The subsequent independent source judgment receives 160,743 initial prompt bytes,
compared with 262,965 in the unsplit synthesis. Its decision remains pending at
this checkpoint. This finding is evidence of a protocol/history bottleneck in
addition to latency, not evidence of an RL improvement or completed priorities1–4.
The stage-cache and original-history preservation regression passed along with
the existing control/review checks, 42 in total.

## Routing unresolved scope before code review

The first split code review did finish, rejecting the controller/grid scope while
finding the four bound outputs emitted. Its response incorrectly cited the
derived scope guide as an authoritative blocking basis, and host validation
rejected that citation. The coordinator started correcting the reviewer response;
that continuation was interrupted while fixing the stage transition.

An unresolved protocol-scope assessment now stops before code judgment and is
stored on the ResearchWork. The work stays planned with `requires_replanning`,
points to `plan_research_work`, and permits reconsideration from that evidence.
No experiment, negative scientific result or forced new causal diagnostic is
created by a scope ambiguity. The accepted source is retained. A scope guide
with an answered status remains separate from implementation approval.

For later code reviews, literal guide references and quotes are resolved back to
canonical protocol-note paths only when the quote exactly occurs there. These
verified clause citations are provided to the reviewer; the derived guide itself
cannot create an authoritative blocking requirement.

`scope_route_recovery.json` records recovery through the actual MCP preflight
handler using the real persisted Luna scope assessment. A transport assertion
prevented any fresh model call during this recovery. The work returned planned,
requires_replanning=true, next_tool_to_call=plan_research_work and no new
observation. The `scope-route-recovery` supervisor continuation then entered live
Luna planning with the historical result index and scope feedback, 172,650 initial
prompt bytes. 44 control/review checks passed, including unresolved-scope caching,
no code-review call on unresolved scope, reconsideration, and no fabricated
measurement. The autonomous RL intervention/comparison remains outstanding.

### Early comparison execution route and consumable scope (07:49 KST)

The planner could select an exploratory comparison before baseline qualification, but measurement-only preflight rejected comparison and replication kinds. Extended the existing operational preflight route to those kinds while retaining source/protocol review, budget/partition enforcement, no baseline qualification and inconclusive formal claim output. Planner policy 24 and the MCP description now expose this actual capability.

The scope interpreter previously received protocol text but no execution history. It could therefore read an exactly-once permission as still available after its execution. It now receives preparation execution objectives, statuses and receipt digests, including unsuccessful launches, and must distinguish scope applicability from permission consumption. No host scientific interpretation or source was supplied. Review policy 13 invalidates reviews missing this dependency.

Observed latest autonomous selection ca637fba repeated a collect-and-return proposal; its dispatch was rejected for reusing an existing node with a different plan. Existing completed collect-return record n_preflight_collect_return_feasibility_04 was present in the historical measurement index but not cited by that selection. This is not research progress or priority-4 completion. Current preparation runtime is 14115.46 seconds over 100 recorded runs, below the registered eight-hour ceiling.

Verification: `venv/bin/python -m pytest tests/test_research_control.py tests/test_research_review.py -q --tb=short` — 46 passed. Actual LocalRunner route coverage now includes unqualified comparison/replication with no scientific claim approval. This narrow evidence does not demonstrate an autonomous intervention comparison.

### Distinguishing missing measurement from missing generation fields

Live policy-24 planner work 5575d76d cited the earlier completed feasibility record but chose another run because that record lacked the three newly named support counts. Its exact reasoning is preserved in work.json; this is not yet a successful intervention choice. The selector's detailed measurement context still contained only the latest two records, even when its preceding decision explicitly cited an older measurement. Detailed raw values/pointers now also include measurements cited by the preceding work. This exposes original data for source analysis without inventing count equivalence or retroactively approving old evidence.

A second remaining conflation set diagnostic_required for any rejected/interrupted dispatch, including no-launch request errors. Removed that condition. Actual failed measurements and duplicate observations retain their existing handling; no-launch feedback remains in the authoritative outcome and implementation/protocol review. Extended existing reconsideration coverage to actual pre-execution rejections and verified they do not force a scientific diagnostic.

Verification: combined research_control/research_review checks: 46 passed. Policy-24 live run remains in protocol-scope interpretation for the proposed control; the model process already in flight is not restarted simply to discard paid work. These changes will enter its next process boundary.

### Known scope prohibition must not pay for source review

In run luna-cycle-e2e-20260907-074611, the live scope interpretation answered that the original one-use control permission was consumed by completed run _04. Because its analysis status was answered rather than unresolved, the old route nevertheless started code review. That redundant review timed out after 300 seconds and stopped the bounded batch. No new game ran.

Added an explicit protocol-scope disposition: eligible_for_source_review, blocked, unresolved. A blocked result requires a verbatim original-clause citation and routes directly to replanning; eligibility remains neither code approval nor execution authorization. Both ambiguous and established-blocked scope preserve their receipts and bypass source review. Invalid blocked citations are recorded as reviewer contract errors for correction rather than cached as accepted scientific findings. Review policy 14 prevents stale source approvals bypassing this dependency.

Verification: `venv/bin/python -m pytest tests/test_research_control.py tests/test_research_review.py tests/test_schema_validation.py -q --tb=short` — 52 passed. The existing scope recovery case now covers established prohibition as well as ambiguity and eligible scope. An initial command named nonexistent tests/test_schemas.py and ran no checks; the corrected command above is the reported validation. The old bounded budget and failed-call receipt are preserved before continuing with the changed route.

### Live scope recovery and historical analysis selection

The resumed Luna scope call returned answered/blocked for work 5575d76d. Actual MCP routing preserved the scope receipt, set requires_replanning, and started a planner without another source-review call or game launch. The next planner input had diagnostic_required=false and detailed measurement facts for completed collect-return _04. Luna selected existing-source analysis 27ce0db8352b27 instead of another fresh-node control. Its initial analysis input is 26359 bytes.

The selected question is still narrower than the scientific goal: it asks whether an older artifact has three newly named support counts, even though its saved six-cell telemetry already reports completion, pickups and returns. The planner guidance now explicitly distinguishes work-local support bindings from a universal gate on using historical evidence. It permits transparent derivation from saved records with traced source provenance, without altering old bindings or retroactively qualifying results. It also asks whether remaining diagnostic measurements can be integrated into a small matched intervention rather than becoming another separate prerequisite. Invalid support still makes the affected effect uninterpretable. This is generic research guidance; no Pacman algorithm or scientific intervention was supplied by the root.

Verification for this instruction-only adjustment: git diff --check. Behavioral E2E continuation remains the acceptance evidence; the new analysis is not priority-4 completion.

### Preserve a decision when an evidence ID is mistyped

The next Luna decision selected a prospective support-design protocol revision, but its previous_result.evidence_ids contained a malformed analysis hash. Canonical validation rejected the decision. The supervisor then tried advance_research, treating a reference error as grounds for a fresh research direction. The owned supervisor was stopped; current completed analysis 27ce0db8352b27 was preserved.

Planner generation now constrains both evidence-reference arrays to the actual available IDs. Saved otherwise schema-valid decisions with invalid reference tokens enter a small, tool-free citation-repair call: Luna maps only those tokens to existing IDs or declines identification. The host preserves every other decision field and then runs normal semantic validation. Failed identification falls back to full planning, never a guessed source; correction receipts are cached so metadata recovery does not repeat paid calls.

advance_research now routes an unaccepted planner response for the current work back to plan_research_work before preparing a new direction command. Accepted command receipts remain idempotent. A live MCP call against the failed thread returned planning_required with the original work/failed-response receipt and no new observation; receipt is planning_route_verification.json in the 081449 batch.

Verification: research_control, research_review, thread_supervisor — 107 passed, 3 subtests passed; git diff --check. New narrow coverage checks citation-only recovery, uncertain-reference fallback/cache, and no direction restart after an unaccepted decision. Actual live citation repair and research progress remain pending.

### Live citation recovery; protocol review input boundary

The live citation repair corrected exactly one malformed analysis ID and the normal planner accepted work 400134fb6db4aa4f, a protocol revision. Comparing rejected and accepted JSON after applying only the saved replacement mapping proved every other decision field unchanged. Repair initial prompt: 18802 bytes versus 152915 for the failed full decision. Recorded repair usage: 31194 input tokens, 252 output tokens including 154 reasoning tokens. Prompt byte reduction is not the same as billed token reduction.

Before the next protocol review, its existing builder was found to include all analysis findings (140279 bytes), chronological protocol history (107181), diagnostic bindings (31942), inventory (10445) and development results (6271), before the proposal and other fields. This exceeds the 280KB call limit without needing a paid model attempt. Prospective protocol review now receives selected findings inline, keeps full protocol history and the proposal inline, and retains all other material as hashed addressable references. Source-inspection recovery also applies to these protocol reviews, preserving completed reads if interpretation times out.

Verification: research_control/research_review — 51 passed. Extended the existing interrupted-inspection case to a protocol amendment, checking preserved full history, selected findings, complete referenced archive, and tool-free resumed synthesis. git diff --check passed. Live protocol amendment approval and intervention execution remain outstanding.

### Protocol checkpoint preserves the submitted amendment

The actual protocol review prompt was 371250 bytes and rejected before model submission. The unchanged saved proposal through the new bundle measures 188207 bytes. Replaying that exact saved proposal while its existing budget remained exhausted produced a checkpoint, preserved the registered protocol byte-for-byte as JSON, and added zero model calls; protocol_checkpoint_recovery.json records this.

The previous protocol MCP handler classified budget/transport interruption as rejected and enabled reconsideration. It now records a checkpoint, saves exact submitted amendment arguments, and exposes an unchanged resume handoff. Completed independent review clears the checkpoint; actual design rejection still follows its normal revision path. Existing empirical saved-request handoffs also cover comparison/competence work, not just diagnostics.

Verification: research_control/research_review — 51 passed. Extended existing protocol-amendment cases across study-design, component-binding and partition variants to assert that budget interruption changes no protocol and preserves exact retry arguments. git diff --check passed. No new game or intervention result yet.

### Remove unconditional sampler re-registration

The resumed coordinator preserved notes but changed replace_holdout/defer_holdout_generation from the saved false/false to true/true. A global supervisor instruction said to retire/re-register whenever a future sampler was present, contradicting exact checkpoint replay. Replaced that unconditional instruction: only a selected new sampling design uses those flags; mere presence of an already registered sampler does not. The in-flight independent review still owns the submitted revision; the root has not altered its scientific proposal.

Verification: included in the combined research_control/research_review/thread_supervisor run — 108 passed, 3 subtests passed; git diff --check. Live recovery fidelity after this prompt adjustment remains to be observed at the next process boundary.

### Source analysis must be able to update a source question

prediction_support returned no eligible measurement counts for analysis work, and the generated planning schema consequently forced every source-analysis alternative to unresolved even after an answered analysis. Thus a completed inspection could not change a competing interpretation in the next decision. This was a generation-contract defect, not an absence of model reasoning.

Answered analysis_completed work now carries answered_source_analysis as its support basis and may update its own alternatives with source-receipt citations and an empty observation-ID list. Unresolved analysis, failed executions, absent empirical support, and zero counts keep their existing restrictions. This neither supplies empirical measurements nor qualifies a scientific claim. The existing schema/semantic validation case now demonstrates an informative update from answered source analysis while retaining rejection of invented observation IDs and unsupported empirical effects.

Verification: research_control/research_review/thread_supervisor — 108 passed, 3 subtests passed; git diff --check. Historical interpretations are not rewritten by the host.
