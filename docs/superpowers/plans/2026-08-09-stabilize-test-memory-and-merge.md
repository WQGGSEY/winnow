# Stabilize Test Memory and Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the test suite independent of ignored operator-local memory, update the stale connector contract test, and fast-forward the completed branch into `main` only after a green suite.

**Architecture:** Immutable bootstrap research artifacts live inside the `research_harness.memory` package. Operator-local `memory/` remains ignored and overlays packaged defaults, so real runs retain their mutable history while clean checkouts and tests have deterministic inputs. Tests that only exercise YAML parsing use inline temporary fixtures instead of runtime memory.

**Tech Stack:** Python 3.12, unittest/pytest, package data in `pyproject.toml`, Git fast-forward merge.

## Global Constraints

- Keep operator-local `memory/` ignored and do not overwrite its current contents.
- Prefer operator-local baseline dossiers over packaged bootstrap dossiers with the same ID.
- Merge built-in failure seeds with operator-local failure records without duplicating paths.
- Use `umask 0002`; new regular files must be mode 664 and directories mode 775.
- Do not merge into `main` until the complete test suite passes before and after the merge.

---

### Task 1: Package a Bootstrap Baseline Dossier

**Files:**
- Modify: `.gitignore`
- Create: `research_harness/memory/baseline_dossiers/bd_agent_harness_20260523.yaml`
- Create: `research_harness/memory/baseline_dossiers/candidates/c1_sakana_ai_scientist_v2.md`
- Create: `research_harness/memory/baseline_dossiers/candidates/c2_direct_api_port.md`
- Create: `research_harness/memory/baseline_dossiers/candidates/c3_no_orchestrated_harness.md`
- Modify: `research_harness/memory/baseline_dossier.py`
- Modify: `research_harness/workers/claude_code_invoker.py`
- Modify: `pyproject.toml`
- Test: `tests/test_baseline_dossier.py`

**Interfaces:**
- Consumes: operator-local `<repo>/memory/baseline_dossiers/<id>.yaml` when present.
- Produces: `dossier_path(repo_root, dossier_id) -> Path` resolving local-first, packaged-second; `load_baseline_dossier` validates detail files relative to the resolved dossier directory.

- [ ] **Step 1: Add a failing clean-root fallback test**

```python
def test_packaged_dossier_is_available_without_operator_memory(self):
    with tempfile.TemporaryDirectory() as tmp:
        dossier = load_baseline_dossier(Path(tmp), "bd_agent_harness_20260523")
    self.assertEqual(dossier["selected"]["candidate_id"], "c1_sakana_ai_scientist_v2")
```

- [ ] **Step 2: Run the focused test and verify it fails with `baseline dossier not found`**

Run: `venv/bin/python -m pytest tests/test_baseline_dossier.py -q -p no:cacheprovider`

- [ ] **Step 3: Add packaged dossier files and local-first resolution**

Use the last tracked canonical dossier from commit `955ce15^`; change `validate_baseline_dossier` to validate candidate files relative to the resolved dossier's parent. Change `ClaudeCodeInvoker._baseline_context` to call `load_baseline_dossier` instead of constructing a mutable-memory path.

- [ ] **Step 4: Register the YAML and Markdown files as package data**

Add `baseline_dossiers/*.yaml` and `baseline_dossiers/candidates/*.md` under `research_harness.memory` in `pyproject.toml`.

- [ ] **Step 5: Run baseline, invocation-envelope, pipeline, and preflight tests**

Run: `venv/bin/python -m pytest tests/test_baseline_dossier.py tests/test_invocation_envelope.py tests/test_pipeline.py tests/test_local_preflight.py -q -p no:cacheprovider`

### Task 2: Make Failure Memory an Overlay

**Files:**
- Create: `research_harness/memory/default_failures/index.yaml`
- Create: `research_harness/memory/default_failures/invalid_experiment/n_demo_001__invalid_experiment__01330e757f.md`
- Create: `research_harness/memory/default_failures/invalid_experiment/n_demo_001_b01__invalid_experiment__544d46585c.md`
- Modify: `research_harness/memory/failure_retrieval.py`
- Modify: `research_harness/memory/failure_memory.py`
- Modify: `research_harness/workers/claude_code_invoker.py`
- Modify: `pyproject.toml`
- Test: `tests/test_failure_retrieval.py`
- Test: `tests/test_branch_prior.py`

**Interfaces:**
- Produces: `load_failure_index(repo_root) -> dict[str, Any]`, merging packaged seed entries with operator-local records; retrieval resolves each indexed file local-first, packaged-second.
- Preserves: new failure records are written only under operator-local `memory/failures/`.

- [ ] **Step 1: Add a failing no-local-memory retrieval test**

```python
def test_packaged_failures_are_retrievable_without_operator_memory(self):
    with tempfile.TemporaryDirectory() as tmp:
        summaries = retrieve_failure_summaries(
            Path(tmp), query_tags=["agent_harness", "subscription_oauth"],
            selected_fail_files=[], top_k=2,
        )
    self.assertEqual(len(summaries), 2)
```

- [ ] **Step 2: Verify the test fails because `memory/failures/index.yaml` is absent**

Run: `venv/bin/python -m pytest tests/test_failure_retrieval.py -q -p no:cacheprovider`

- [ ] **Step 3: Implement packaged defaults plus local overlay**

Load both indexes, preserve canonical category order, de-duplicate file paths, and resolve records from local memory before package defaults. Initialize an empty local category index when recording the first runtime failure.

- [ ] **Step 4: Route prompt context through the merged index**

Replace `ClaudeCodeInvoker._failure_context`'s direct local `load_yaml` with `load_failure_index`.

- [ ] **Step 5: Run failure retrieval, branch prior, invocation, and live workflow tests**

Run: `venv/bin/python -m pytest tests/test_failure_retrieval.py tests/test_branch_prior.py tests/test_invocation_envelope.py tests/test_live_gate.py tests/test_live_dispatch.py tests/test_live_smoke_runner.py -q -p no:cacheprovider`

### Task 3: Remove Stale Test Contracts

**Files:**
- Modify: `tests/test_config_loader.py`
- Modify: `tests/test_connector_reading_prune1.py`
- Modify: `tests/test_connector_orchestrator.py`
- Modify: `tests/test_connector_reduction_market.py`
- Modify: `tests/test_failure_memory.py`
- Modify: `tests/test_failure_retrieval.py`
- Modify: `tests/test_live_memory_update.py`
- Modify: `tests/test_lesson_distillation.py`
- Modify: `tests/test_market_research.py`

**Interfaces:**
- Tests the YAML parser with a literal temporary list-of-maps document.
- Tests the current connector contract: `field_mechanism` and `predicted_behavior`.
- Builds every mutable-memory fixture under a temporary repository and never reads,
  writes, or deletes operator-local root `memory/`.

- [ ] **Step 1: Replace the config test's runtime-memory path with an inline YAML fixture**

The assertion remains on parsed list-of-map behavior, not repository state.

- [ ] **Step 2: Update the reading fake and assertions to the mechanism contract**

Assert `field_mechanism == "replicator dynamics"` and a non-empty `predicted_behavior`; update prune fixtures to the same field names.

- [ ] **Step 3: Run both focused test modules**

Run: `venv/bin/python -m pytest tests/test_config_loader.py tests/test_connector_reading_prune1.py -q -p no:cacheprovider`

- [ ] **Step 4: Reproduce and remove clean-checkout memory dependencies**

Materialize the staged index with `git checkout-index`, run the complete suite
there, and replace every ignored root-memory fixture with packaged or temporary
data. Assert root `memory/` remains absent before and after the run.

### Task 4: Verify, Commit, and Fast-Forward

**Files:**
- Modify: `pyproject.toml`
- Verify all modified files and generated package-data modes.

**Interfaces:**
- Produces: a green feature branch and a local `main` pointing to the same commit.

- [ ] **Step 1: Run the complete suite on the feature branch**

Run: `PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider`

- [ ] **Step 2: Declare the complete test environment**

Add a `test` optional dependency group containing pytest, matplotlib,
Starlette's preferred `httpx2` test client, PyYAML, and the frontend dependencies
needed during suite collection; install it and require a warning-free full-suite
run.

- [ ] **Step 3: Review diff and commit the test-stability fix**

Run: `git diff --check`, inspect `git diff`, stage only plan/bootstrap/code/test files, and commit with `fix: make ignored research memory test-safe`.

- [ ] **Step 4: Fast-forward `main`**

Run: `git switch main && git merge --ff-only WQGGSEY/declared-vs-measured-rev2`.

- [ ] **Step 5: Re-run the complete suite on merged `main`**

Run: `PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider`

- [ ] **Step 6: Delete only the merged local feature branch**

Run: `git branch -d WQGGSEY/declared-vs-measured-rev2`; leave the remote branch and `origin/main` unchanged.
