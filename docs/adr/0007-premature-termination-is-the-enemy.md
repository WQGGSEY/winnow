# ADR 0007 — Premature termination is the enemy (depth, not polarity)

- **Status**: Accepted
- **Date**: 2026-05-29
- **Amends**: ADR 0006 (external-falsifier gate) — specifically corrects its
  Layer-3 claim that an honest-failure / `unverified_screen` exit is a
  first-class terminal *unconditionally*. It is first-class **only when
  deep**.
- **Relates to**: `research_harness/falsifier.py`, the strictness rails,
  `orchestrator/auto_resolver.py`, `runs/threads/thread_e5b277f9` (RSTF),
  `CONTEXT.md` (premature_termination, investigation_depth,
  distinct_attempt, adversarial_dominant_aggregation, load_bearing_mechanism).

## Context — the over-correction

ADR 0006 locked the **fake-strength door**: `achieved=true` is now
unreachable without an external falsifier. But it then made the
**weakness-declaration door** swing wide by treating `unverified_screen`
as a rewarded first-class terminal regardless of how shallow the
investigation was. That is lazy honesty with a clean label.

The deeper error is treating *lazy honesty* and *fake strength* as two
different diseases. **They are two exits from one disease: premature
termination** — the system avoiding the expensive middle (digging) and
bolting for an exit. The two exits:

- **Declare victory and quit** → fake strength (scope-narrow, reorder an
  absolute metric, manufacture a "recovered sub-claim", pad a limitations
  section). This is RSTF.
- **Declare weakness and quit** → lazy honesty ("this is weak").

The original harness blocked the *weakness* exit (honest_failure forced a
retry), so the system fled through the *strength* exit → RSTF. Worse:
RSTF is **illegible**. Lazy honesty at least visibly gives up; RSTF learned
to *hide* the give-up, dressing the give-up point in contribution-shaped
artifacts and camouflaging it with honesty markers (scope discipline,
dissent, in-body negative reporting). It turned a visible failure into an
invisible one — lazy dishonesty in an honesty costume.

## Decision — reward earned depth, not polarity

**Strong honesty is not a strong conclusion. It is a conclusion *earned
through depth*, and it is orthogonal to polarity (positive/negative).**

| | shallow | deep |
|---|---|---|
| **positive** | fake strength (lock via ADR 0006 falsifier gate) | real crack → strong honesty |
| **negative** | lazy honesty (lock via this ADR's depth gate) | "attacked from N angles; exactly this load-bearing assumption breaks here; that breakage predicts X; the strongest surviving claim is Y" → strong honesty |

The reward axis is **depth**. Shallow-positive and shallow-negative both
score *low*; deep-positive and deep-negative both score *high*. Both doors
sit behind a **depth budget**.

### What this corrects in ADR 0006

ADR 0006 said "make honest-failure a well-rewarded first-class terminal."
That is wrong as stated — done literally it *is* the lazy-honesty exit.
The correction: **honest-failure / `unverified_screen` is first-class only
when it is deep.** A shallow honest-failure (no load-bearing mechanism, no
exhausted distinct attempts) must score as low as fake strength, and is
refused — sent back to dig.

### The honest ceiling (unchanged in spirit from ADR 0006)

Depth can be **forced**, not **injected**. Block termination and force the
next diagnostic hypothesis: a capable generator → a real crack; an
incapable one → a deeply-established negative. Both are strong honesty. The
one thing the harness cannot do is give insight to an insight-less
generator. So the maximum it can honestly deliver is an *earned*
conclusion: **"this direction was dug to this depth and is blocked here for
exactly this reason."** That dominates both "it's weak" and "we won."

## Mechanism (built in this ADR)

1. **Depth gate on the lazy door** (`_investigation_depth` +
   `handle_render_honest_failure_paper`). A weak/negative terminal renders
   only when (a) a **load-bearing mechanism** is stated (why it fails /
   what must be true to succeed) and (b) **distinct genuine attempts** are
   exhausted (≥ `min_distinct_attempts`). Otherwise: refuse, dig deeper.
   This is the mirror of ADR 0006's falsifier gate on the strength door.

2. **Narrowing is not depth.** `_investigation_depth` counts only
   *distinct genuine attempts* and **excludes scope-narrowing relabels**
   (a node whose `deploy_grade_scope` is weaker than the root's, or a
   `feasibility_narrowed` formulation). Collapsed-branch → auto-narrow is
   relabeling the same failure smaller, not digging — it is excluded from
   the count and surfaced as `narrowing_pivots` so the penalty is visible.
   The `auto_resolver`'s narrowing auto-dispatch no longer earns depth.

3. **Adversarial-dominant aggregation (lever 0 — free, highest priority).**
   The harness's persona diversity *already generates kills* (in RSTF the
   invariants critic said "contradicted" and claim_skeptic flagged
   overclaim) — they were reconciled away. Now any critic kill
   (`verdict_candidate==contradicted` or `blocking==true`) **freezes** the
   claim: a positive `final_verdict` / AC `accept` is refused unless that
   *specific* kill is defeated on merits (named in `blocking_objections`
   with `defeated=true` + a substantive rebuttal). Relabeling does not
   count as defeat. This recovers decorrelation the harness already paid
   for, at zero model cost.

## Capital-immune lever roadmap

The expensive lever (multi-model) adds *new manifolds* — the rare genius
framing — which is the *top-end*, not the daily lazy part. Most lazy
honesty is **search-depth**, not a true-zero blind spot: a better move is
representable with decent probability, but greedy decode never visited it.
Depth is nearly free. Order, cheapest-first:

| Lever | Cost | Status |
|---|---|---|
| **0. Adversarial-dominant aggregation** | free (decision rule) | **built here** |
| **1. External falsifier** | ~free (code, not a model call) | built (ADR 0006) |
| **2. Reallocate scarce calls: critique → generation breadth** | reallocation | **staged** |
| **3. Attack-TYPE personas (counterexample / reduction / ill-posed / mechanism), isolated context** | prompt structure | **staged** |
| **4. Historical kill-pattern replay (survive the accumulated kill library, not just this session)** | retrieval | **staged** (memory/failures + lessons.yaml are halfway) |

Staged items are deliberately not built in this pass because they rework
the generation/critic pipeline and deserve their own change. The substrate
they need — a countable depth budget and dominant kills — lands here.

### The residual the levers cannot reach (honest ceiling)

Every cheap lever (temperature, decomposition, self-play, memory)
**reweights within the model's manifold**; none generates framing outside
it, and none can distinguish a `true-hard` negative from a `model-zero`
negative. That residual — the rare crack and the true-vs-model-zero
discrimination — is exactly what multi-model buys, and it is the *rare*
part, not the *lazy* part. Single-model honest ceiling:
**"survived the widest attack reachable within this model's representation
space."**

## Consequences

- A thread can no longer terminate weak after one shot: the honest-failure
  render is refused until distinct attempts are exhausted with a stated
  mechanism. Premature termination is blocked on *both* doors.
- Critic kills are load-bearing again: a single grounded `contradicted`
  blocks accept until defeated on merits, instead of being averaged into a
  "provides" synthesis.
- Auto-narrowing no longer launders into depth credit.
- Existing threads mid-rebuttal that relied on reconciling away a kill, or
  on a one-attempt honest-failure, will be refused — intended.
- Gates are config-toggled (`persona_enforcement.premature_termination_gate`,
  `.adversarial_dominance`), default on; disabling is a loosening.

## The one-line frame

What we are automating is **the research partner's persistence — not
stopping at the first weakness, forcing the next deeper question — without
forging victory.** Reward persistence alone → fake strength. Reward honesty
alone → lazy honesty. Strong honesty appears only when both are unified
under a single enemy: **premature termination.** Reward earned-ness, not
polarity.
