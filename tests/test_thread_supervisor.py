"""Tests for thread_supervisor — hands-free e2e daemon.

We don't actually spawn `claude` subprocesses in tests; we exercise:
  - is_terminal() against various production_run_summary.json shapes
  - mcp_idle_seconds() against various mtime patterns
  - build_resume_prompt() output shape
  - SupervisorLock contention + stale lock recovery
  - watch_thread() loop with mocked spawn (verify terminate, idle-spawn,
    max-cycles, SIGINT)
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from research_harness import thread_supervisor as ts


def _make_thread(repo: Path, tid: str) -> Path:
    tdir = repo / "runs" / "threads" / tid
    (tdir / "production").mkdir(parents=True, exist_ok=True)
    return tdir


class TerminalDetectionTests(unittest.TestCase):
    def test_no_summary_file_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)
            self.assertIsNone(outcome)

    def test_honest_failure_outcome_is_NOT_terminal_under_pr8(self):
        # PR8: honest_failure is a retreat state, not a terminal one.
        # supervisor must keep iterating until dual-gate passes.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_accept_without_attestation_is_NOT_terminal_under_pr8(self):
        # PR8: AC accept alone is no longer enough. Dual-gate requires
        # Professor user_goal_attestation.achieved=true.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_dual_gate_pass_is_terminal(self):
        # PR8: only this combination terminates the supervisor.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": True, "what_user_can_do_with_this_paper": "x"}),
                encoding="utf-8",
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertTrue(t)
            self.assertEqual(outcome, "accept_with_goal_achieved")

    def test_accept_without_artifacts_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {"rendered_artifacts": []},
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_attestation_achieved_false_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": False}), encoding="utf-8"
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)

    def test_corrupt_summary_is_not_terminal(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                "not json", encoding="utf-8"
            )
            t, outcome = ts.is_terminal(repo, "t1")
            self.assertFalse(t)


class IdleDetectionTests(unittest.TestCase):
    def test_empty_production_dir_idle_is_inf(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            self.assertEqual(ts.mcp_idle_seconds(repo, "t1"), float("inf"))

    def test_missing_production_dir_idle_is_inf(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(ts.mcp_idle_seconds(Path(tmp), "no_thread"), float("inf"))

    def test_recent_file_yields_small_idle(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            f = tdir / "production" / "tree" / "search_state.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
            idle = ts.mcp_idle_seconds(repo, "t1")
            self.assertLess(idle, 5.0)

    def test_old_file_yields_large_idle(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            f = tdir / "production" / "tree" / "search_state.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}", encoding="utf-8")
            past = time.time() - 7200
            os.utime(f, (past, past))
            idle = ts.mcp_idle_seconds(repo, "t1")
            self.assertGreater(idle, 3600)


class ResumePromptTests(unittest.TestCase):
    def test_resume_prompt_carries_thread_id_and_anti_lazy_brief(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_thread(repo, "t1")
            prompt = ts.build_resume_prompt(repo, "t1", cycle=3)
            self.assertIn("t1", prompt)
            self.assertIn("cycle #3", prompt)
            self.assertIn("dual-gate", prompt.lower())
            self.assertIn("honest_failure", prompt)
            self.assertIn("anti-laziness", prompt.lower())

    def test_resume_prompt_reads_state_when_present(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            state = {
                "promoted_node_ids": ["n_root"],
                "pruned_node_ids": [],
                "status": "running",
                "nodes": [
                    {"id": "n_root", "status": "promoted"},
                    {"id": "n_a", "status": "ready"},
                ],
            }
            (tdir / "production" / "tree").mkdir(parents=True, exist_ok=True)
            (tdir / "production" / "tree" / "search_state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            prompt = ts.build_resume_prompt(repo, "t1", cycle=1)
            self.assertIn("n_root", prompt)


class LockTests(unittest.TestCase):
    def test_lock_acquire_and_release(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            lock = ts.SupervisorLock(tdir)
            lock.acquire()
            self.assertTrue((tdir / ".supervisor.lock").exists())
            lock.release()
            self.assertFalse((tdir / ".supervisor.lock").exists())

    def test_stale_lock_is_cleared(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # 99999999 is virtually guaranteed to be a dead PID.
            (tdir / ".supervisor.lock").write_text("99999999", encoding="utf-8")
            lock = ts.SupervisorLock(tdir)
            lock.acquire()
            # After acquire, the lock should hold THIS process's pid.
            self.assertEqual(
                (tdir / ".supervisor.lock").read_text().strip(), str(os.getpid())
            )
            lock.release()

    def test_live_lock_blocks(self):
        with TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # current process's pid is alive -> should block.
            (tdir / ".supervisor.lock").write_text(str(os.getpid()), encoding="utf-8")
            lock = ts.SupervisorLock(tdir)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                lock.acquire()
            # cleanup
            (tdir / ".supervisor.lock").unlink()


class WatchLoopTests(unittest.TestCase):
    def test_dual_gate_pass_exits_terminal(self):
        # PR8: only dual-gate pass terminates.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": True}), encoding="utf-8"
            )
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({
                    "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                    "publication_dispatch": {
                        "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                    },
                }),
                encoding="utf-8",
            )
            with mock.patch.object(ts, "spawn_claude_session") as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01, max_cycles=3
                )
            spawn.assert_not_called()
            self.assertEqual(result["status"], "terminal")
            self.assertEqual(result["outcome"], "accept_with_goal_achieved")

    def test_honest_failure_alone_does_NOT_terminate_under_pr8(self):
        # PR8: honest_failure is a retreat state. Supervisor keeps trying.
        # We use max_cycles=2 as emergency override to bound the test.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "production_run_summary.json").write_text(
                json.dumps({"outcome": "honest_failure"}), encoding="utf-8"
            )
            with mock.patch.object(ts, "spawn_claude_session", return_value=0) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01,
                    max_cycles=2, rate_limit_backoff_initial=0.001,
                )
            # spawn was called twice — supervisor kept trying despite honest_failure.
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_idle_thread_spawns_until_dual_gate(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            summary_path = tdir / "production" / "production_run_summary.json"
            attestation_path = tdir / "production" / "rebuttal" / "user_goal_attestation.json"

            def fake_spawn(prompt, **kw):  # noqa: ARG001
                attestation_path.parent.mkdir(parents=True, exist_ok=True)
                attestation_path.write_text(
                    json.dumps({"achieved": True}), encoding="utf-8"
                )
                summary_path.write_text(
                    json.dumps({
                        "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                        "publication_dispatch": {
                            "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                        },
                    }),
                    encoding="utf-8",
                )
                return 0

            with mock.patch.object(ts, "spawn_claude_session", side_effect=fake_spawn) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01,
                    max_cycles=3, rate_limit_backoff_initial=0.001,
                )
            spawn.assert_called_once()
            self.assertEqual(result["status"], "terminal")

    def test_max_cycles_override_can_force_exit(self):
        # Emergency operator override still works for safety.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            with mock.patch.object(ts, "spawn_claude_session", return_value=0) as spawn:
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.01,
                    max_cycles=2, rate_limit_backoff_initial=0.001,
                )
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(result["status"], "max_cycles_exceeded")

    def test_unlimited_cycles_by_default(self):
        # Default (no max_cycles) = supervisor never quits on count.
        # We assert via lots of spawns + manual interrupt via mock.
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            count = {"n": 0}
            attestation_path = tdir / "production" / "rebuttal" / "user_goal_attestation.json"

            def fake_spawn(prompt, **kw):  # noqa: ARG001
                count["n"] += 1
                # only complete on the 20th cycle.
                if count["n"] >= 20:
                    attestation_path.parent.mkdir(parents=True, exist_ok=True)
                    attestation_path.write_text(json.dumps({"achieved": True}), encoding="utf-8")
                    (tdir / "production" / "production_run_summary.json").write_text(
                        json.dumps({
                            "rebuttal_summary": {"ac_decision": {"decision": "accept"}},
                            "publication_dispatch": {
                                "rendered_artifacts": [{"output": "paper_html", "artifact_path": "/x"}]
                            },
                        }),
                        encoding="utf-8",
                    )
                return 0

            with mock.patch.object(ts, "spawn_claude_session", side_effect=fake_spawn):
                result = ts.watch_thread(
                    repo, "t1", max_idle_seconds=0.0, poll_seconds=0.001,
                    rate_limit_backoff_initial=0.001,
                    rate_limit_backoff_max=0.01,
                )
            self.assertEqual(result["status"], "terminal")
            self.assertGreaterEqual(count["n"], 20)

    def test_thread_dir_missing_raises(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "not found"):
                ts.watch_thread(Path(tmp), "no_such_thread")


class EnvelopeAutoBootstrapTests(unittest.TestCase):
    def _setup_repo(self, tmp: str, *, with_market: bool = True, registered_adapters=None):
        repo = Path(tmp)
        (repo / "runs" / "threads" / "t1" / "production").mkdir(parents=True)
        if with_market:
            mdir = repo / "runs" / "threads" / "t1" / "market"
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "market_research_brief.json").write_text(json.dumps({
                "baseline_dossier_candidates_index": [
                    {"id": "c1", "arxiv_id": "2401.12345"},
                    {"id": "c2", "filename": "paper.pdf"},
                ]
            }), encoding="utf-8")
        settings = {
            "data_adapters": {
                "registered": registered_adapters or []
            }
        }
        (repo / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        return repo

    def test_bootstrap_writes_schema_valid_envelope(self):
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp, registered_adapters=[
                {"id": "wq_snap", "provenance": "operator-curated"}
            ])
            env = ts.bootstrap_envelope_if_missing(repo, "t1", target_scope="directional")
            self.assertIsNotNone(env)
            self.assertEqual(env["thread_id"], "t1")
            # real adapter from settings shows up
            real_ids = [s["id"] for s in env["data_sources_available"] if s["kind"] == "real_adapter"]
            self.assertIn("wq_snap", real_ids)
            # synthetic always present as fallback
            synth_ids = [s["id"] for s in env["data_sources_available"] if s["kind"] == "synthetic"]
            self.assertTrue(synth_ids)
            # baseline provenance pulled from market dossier
            cand_ids = [b["candidate_id"] for b in env["baseline_provenance_available"]]
            self.assertIn("c1", cand_ids)
            # file actually written
            env_path = repo / "runs" / "threads" / "t1" / "production" / "feasibility_envelope.json"
            self.assertTrue(env_path.exists())

    def test_bootstrap_idempotent(self):
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp)
            ts.bootstrap_envelope_if_missing(repo, "t1")
            second = ts.bootstrap_envelope_if_missing(repo, "t1")
            self.assertIsNone(second)  # respects existing envelope

    def test_bootstrap_without_market_dossier_uses_placeholder(self):
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp, with_market=False)
            env = ts.bootstrap_envelope_if_missing(repo, "t1")
            self.assertIsNotNone(env)
            self.assertTrue(env["baseline_provenance_available"])
            self.assertEqual(
                env["baseline_provenance_available"][0]["candidate_id"],
                "no_market_baselines_found",
            )

    def test_bootstrap_target_scope_deployment_with_no_real_adapter_still_writes(self):
        # The envelope is still schema-valid; the validator will reject
        # deployment claims separately at design_initial_claim_contract.
        with TemporaryDirectory() as tmp:
            repo = self._setup_repo(tmp)
            env = ts.bootstrap_envelope_if_missing(repo, "t1", target_scope="deployment")
            self.assertIsNotNone(env)
            self.assertEqual(
                env["operator_intent"]["target_deploy_grade_scope"],
                "deployment",
            )


class NeededResourcesTests(unittest.TestCase):
    def test_needs_collected_from_attestation_when_achieved_false(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({
                    "achieved": False,
                    "required_additional_research": [
                        {"axis": "boundary", "experiment": "SNR sweep", "rationale": "needed"},
                        {"axis": "validity", "experiment": "real data", "rationale": "deploy"},
                    ],
                }),
                encoding="utf-8",
            )
            needs = ts.collect_needed_resources(repo, "t1")
            self.assertEqual(len(needs), 2)
            self.assertEqual(needs[0]["axis"], "boundary")

    def test_envelope_deployment_without_real_adapter_logged_as_blocker(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "feasibility_envelope.json").write_text(
                json.dumps({
                    "operator_intent": {"target_deploy_grade_scope": "deployment"},
                    "data_sources_available": [{"kind": "synthetic", "id": "s1"}],
                }),
                encoding="utf-8",
            )
            needs = ts.collect_needed_resources(repo, "t1")
            self.assertTrue(any(n.get("type") == "data_adapter" for n in needs))

    def test_update_needed_resources_writes_yaml(self):
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tdir = _make_thread(repo, "t1")
            (tdir / "production" / "rebuttal").mkdir(parents=True)
            (tdir / "production" / "rebuttal" / "user_goal_attestation.json").write_text(
                json.dumps({"achieved": False, "required_additional_research": [
                    {"axis": "boundary", "experiment": "x", "rationale": "y"},
                ]}),
                encoding="utf-8",
            )
            needs = ts.update_needed_resources_file(repo, "t1")
            self.assertEqual(len(needs), 1)
            written = (tdir / "needed_resources.yaml").read_text(encoding="utf-8")
            self.assertIn("axis:", written)
            self.assertIn("boundary", written)


class WhichTests(unittest.TestCase):
    def test_which_finds_python(self):
        # Python is always on PATH in CI; sanity-check _which.
        self.assertIsNotNone(ts._which("python3") or ts._which("python"))

    def test_which_returns_none_for_missing_binary(self):
        self.assertIsNone(ts._which("definitely-not-a-real-binary-xyz123"))


if __name__ == "__main__":
    unittest.main()
