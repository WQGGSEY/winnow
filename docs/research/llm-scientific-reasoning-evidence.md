# Evidence on cumulative scientific reasoning by LLM agents

Read 2026-09-06. Primary sources: the three linked arXiv v1 manuscripts, including
methods and the case-study appendices. This is literature analysis, not an
experiment on this harness. Research execution remains stopped.

## Corral: successful execution does not establish scientific reasoning

**Authors' evidence.** Over 25,000 runs cover eight domains, from computational
workflows to hidden-structure inference. Models: GPT-4o-2024-08-06, Sonnet 4.5,
GPT-OSS-120B; scaffolds: ReAct and structured tool calling. Their selected latent
model attributes 41.4% of explained variance to reasoning ability and 1.5% to
scaffold. The epistemic analysis reports evidence non-uptake in 68% of traces and
refutation-driven revision in 26%. It uses ReAct traces, LLM graph annotation,
and agreement checks on 25 traces. This differs from the separate manual
behavioral annotation of 773 traces. Partial successful-trajectory injection
helps workflows more readily than hypothesis-driven tasks.

**Scope.** Two action-expression scaffolds are not the design space of all
research architectures. These results do not establish that no harness can
improve research, or directly evaluate Sol-low/Luna-max. Trace motifs measure
externalized behavior, not inaccessible internal reasoning.

**Our inference.** Evaluate whether contradictory observations change subsequent
predictions and experiment choices; counting completed jobs cannot answer that
question. More context or a reasoning field alone is an insufficient remedy.

Source: [AI scientists produce results without reasoning scientifically,
Results and Methods 4.3–4.9](https://arxiv.org/html/2604.18805v1).

## HEP: persistent hypotheses can evolve, but self-reported belief is not truth

**Authors' evidence.** Three materials tasks investigate structure preferences of
a fixed machine-learned interatomic potential. HEP provides persistent hypotheses,
evidence attachments, refinement, merging, and lifecycle transitions. The
planning-baseline comparison uses GPT-5.5, three runs per condition, and different
stopping criteria. HEP reports multi-generation hypotheses and rules checked on
previously unexamined compositions. The AO2 model comparison uses three runs
each: GPT-5.5, GPT-5.4-mini, GPT-4.1. Mean maximum lineage depth falls from 4.7
to 1.7 to 0.7. Each run has a 24-hour budget; none exhausted it.

**Scope.** Beliefs are agent-elicited, not computed posteriors. The same agent
certifies evidence adequacy. Thresholds 0.8/0.2 enforce declared verdicts, not
calibration. Tool calls directly define much of the measured epistemic cycle.
Physical validity beyond the fixed potential remains unverified; this is not
evidence of autonomous top-conference publication. The manuscript promises code
upon publication.

**Our inference.** Adopt evidence-linked hypothesis ancestry and new predictions,
not numerical self-confidence as qualification. A merged explanation must earn
its value through discriminating predictions, not lineage depth.

Source: [Toward Auditable AI Scientists, Results and
Methods](https://arxiv.org/html/2607.09195v1).

## Four research attempts: failure can change the question

**Authors' evidence.** A six-module pipeline primarily uses Gemini 2.5 Pro, with
Claude Opus 4.1/Sonnet 4 implementing experiments and writing. Four ML ideas
produce three execution/evaluation failures and one Agents4Science 2025
acceptance. Most failures were human-terminated; the revision agent rarely ran.
The successful case changed from proposing a semantic-entropy jailbreak detector
to explaining its failure, generating further hypotheses about confounds and
robustness. A world-model experiment instead reached an uninformative regime:
both methods had near-zero catastrophe rates. Another contained dummy rewards
and severed gradients. The MARL attempt drifted toward prototype code and
misinterpreted a seed paper's proposed extension.

**Scope.** This is a qualitative four-idea case study without systematic
architectural ablations. Humans participated in selection, corrections, and
writing. Its experimental venue acceptance does not establish the requested
hands-off ICML/NeurIPS/ICLR/CVPR/COLT outcome.

**Our inference.** Preserve unexpected scientific failures as opportunities to
change the research question. First establish apparatus validity: broken rewards
are not evidence against a mechanism. A negative result becomes research when
its explanation generates and survives additional tests.

Source: [Why LLMs Aren't Scientists Yet, Sections 1–3, 6, and Appendix
A](https://arxiv.org/html/2601.03315v1).

## Design proposal for discussion, not a literature-proven solution

The missing object is an evolving explanatory model, rather than another task
log. Keep what an explanation accounts for, its assumptions, counterexamples,
and competing explanations. Record how a revision changes those commitments.
Retain the old version so new wording cannot erase a failed prediction.

Allow speculative proposals from analogy, anomalies, or intuition without
pretending they are evidence. Before substantial expenditure, turn a selected
proposal into an observable difference from the current explanation. Budget
exploration separately from validation so unfamiliar ideas survive long enough
to become testable, while attractive stories cannot become conclusions.

When observations repeatedly fail to discriminate, revisit the question,
representation, or measurement regime instead of merely changing algorithms.
For Pacman, candidate explanations might concern symmetry, coordination credit,
or missing basic competence; these are illustrative possibilities, not diagnosed
causes or agent-supplied solutions.

The meaningful eventual check is whether a revised explanation anticipates a
new observation that its predecessor could not, survives counterexamples, and
supports a substantive manuscript contribution. Protocol compliance and a
larger claim graph are supporting checks only.
