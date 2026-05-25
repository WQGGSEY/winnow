from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_harness.agents.grilling import GrillingError, run_grilling_session
from research_harness.config import (
    resolve_agent_budget,
    resolve_agent_max_rounds,
    resolve_agent_model,
)
from research_harness.config import ConfigError
from research_harness.orchestrator.experiment_plan import (
    FALLBACK_TEMPLATE_ID,
    build_experiment_plan_for_node,
    list_available_domains,
    template_directories,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


_VALID_PLAN = {
    "task_class": "smoke_test",
    "objective": "alt domain template",
    "entrypoint": {"command": ["python"], "args": ["src/experiment.py"]},
    "resources": {"timeout_sec": 60, "cpu": 1, "memory_gb": 1, "gpu": None},
    "inputs": {"datasets": [], "snapshots": []},
    "expected_outputs": {
        "metrics_files": ["artifacts/metrics.json"],
        "logs": ["artifacts/run.log"],
        "artifact_dirs": ["artifacts/"],
    },
    "baseline_evidence_requirements": [
        {"role": "current_best_known", "metric_key": "bounded_worker_success_rate",
         "baseline_key": "current_best_known", "operator": "greater_than",
         "margin": 0, "required": True},
        {"role": "naive", "metric_key": "bounded_worker_success_rate",
         "baseline_key": "naive_direct_port", "operator": "greater_than",
         "margin": 0, "required": True},
        {"role": "random_or_null", "metric_key": "bounded_worker_success_rate",
         "baseline_key": "random_or_null", "operator": "greater_than",
         "margin": 0, "required": True},
    ],
}


_EXPERIMENT_PY = (
    "import json\n"
    "from pathlib import Path\n"
    "art = Path('artifacts'); art.mkdir(exist_ok=True)\n"
    "(art/'metrics.json').write_text(json.dumps({\n"
    "    'metrics': {'bounded_worker_success_rate': 0.99},\n"
    "    'baselines': {'current_best_known': 0.8, 'naive_direct_port': 0.4, 'random_or_null': 0.05},\n"
    "    'claim_verdict_candidate': 'supported',\n"
    "    'disproof_conditions_hit': [],\n"
    "    'unexpected_observations': [],\n"
    "}, indent=2))\n"
)


def _stage_template(root: Path, dirname: str, domain: str) -> Path:
    target = root / dirname / domain
    target.mkdir(parents=True, exist_ok=True)
    (target / "plan.json").write_text(json.dumps(_VALID_PLAN))
    src = target / "src"
    src.mkdir(exist_ok=True)
    (src / "experiment.py").write_text(_EXPERIMENT_PY)
    return target


def _make_completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")


def _wrap_assistant_text(text: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": text,
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


class TemplateDirectoryRouterTests(unittest.TestCase):
    def test_template_directories_reads_settings(self) -> None:
        self.assertEqual(template_directories(None), ["experiment_plan_templates"])
        settings = {
            "experiment_plan_templates": {
                "directories": ["my_templates", "shared_templates"]
            }
        }
        self.assertEqual(
            template_directories(settings),
            ["my_templates", "shared_templates"],
        )

    def test_list_available_domains_requires_plan_and_src(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stage_template(root, "primary", "retrieval")
            # broken: plan.json only, no src/
            half = root / "primary" / "halfbaked"
            half.mkdir(parents=True)
            (half / "plan.json").write_text(json.dumps(_VALID_PLAN))
            # secondary directory adds another full template + a duplicate
            _stage_template(root, "secondary", "rl")
            _stage_template(root, "secondary", "retrieval")
            (root / "primary" / "_internal").mkdir()
            (root / "primary" / "not-a-module").mkdir()
            settings = {
                "experiment_plan_templates": {"directories": ["primary", "secondary"]}
            }
            domains = list_available_domains(root, settings)
            self.assertEqual(domains, ["retrieval", "rl"])
            self.assertNotIn("halfbaked", domains)
            self.assertNotIn("_internal", domains)
            self.assertNotIn("not-a-module", domains)

    def test_router_searches_listed_directories_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stage_template(root, "secondary", "altdom")
            settings = {
                "experiment_plan_templates": {"directories": ["primary", "secondary"]}
            }
            from research_harness.orchestrator.demo import _demo_node
            import copy as _copy
            node = _copy.deepcopy(_demo_node())
            node["domain"] = "altdom"
            plan, used = build_experiment_plan_for_node(
                root, node, root / "run", settings=settings
            )
            self.assertEqual(used, "altdom")
            self.assertEqual(plan["objective"], "alt domain template")


class GrillingAllowedDomainsTests(unittest.TestCase):
    def _captured_system_prompt(self, runner_calls: list[dict]) -> str:
        cmd = runner_calls[0]["cmd"]
        sys_index = cmd.index("--system-prompt")
        return cmd[sys_index + 1]

    def test_domain_is_free_form_in_system_prompt(self) -> None:
        """The harness no longer pre-registers domains. Grilling treats the
        domain field as a free-form snake_case tag derived from the
        problem statement; the Professor designs experiment code at
        production entry."""
        captured = []

        def _runner(cmd, *, input, capture_output, text, timeout, check, env):
            captured.append({"cmd": cmd})
            return _make_completed(
                _wrap_assistant_text(
                    json.dumps(
                        {
                            "action": "DONE",
                            "extracted": {
                                "root_goal_id": "rg_x",
                                "domain": "machine_alpha_robustness",
                                "node_type": "capability",
                                "claim_under_test": "x",
                                "mandatory_baselines": ["a"],
                                "success_criteria": ["b"],
                                "disproof_conditions": ["c"],
                                "goal_facets": [],
                                "taste_constraints": [],
                                "search_query_seed": "x",
                            },
                        }
                    )
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            session = run_grilling_session(
                REPO_ROOT,
                user_goal="x",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=_runner,
                input_provider=lambda q: "",
                allowed_domains=["retrieval", "rag"],
            )
            self.assertEqual(session["status"], "done")
            # Free-form domains are accepted even if not in allowed_domains.
            self.assertEqual(session["extracted"]["domain"], "machine_alpha_robustness")
            sysp = self._captured_system_prompt(captured)
            self.assertNotIn("MUST be exactly one of", sysp)
            self.assertIn("free-form", sysp)


class AgentModelResolveTests(unittest.TestCase):
    def test_per_role_overrides_default(self) -> None:
        settings = {
            "runtime": {
                "agent_models": {
                    "default": "haiku",
                    "grilling_agent": "opus",
                    "market_research_agent": "sonnet",
                }
            }
        }
        self.assertEqual(resolve_agent_model(settings, "grilling_agent"), "opus")
        self.assertEqual(resolve_agent_model(settings, "market_research_agent"), "sonnet")
        # role with no entry uses default
        self.assertEqual(resolve_agent_model(settings, "lesson_distillation_agent"), "haiku")

    def test_falls_back_to_live_backend_model(self) -> None:
        settings = {
            "runtime": {
                "worker_backends": {
                    "claude_code_live": {"model": "sonnet"}
                }
            }
        }
        self.assertEqual(resolve_agent_model(settings, "grilling_agent"), "sonnet")

    def test_falls_back_to_literal_sonnet_when_nothing_configured(self) -> None:
        self.assertEqual(
            resolve_agent_model({}, "grilling_agent"), "claude-sonnet-4-6"
        )


class AgentBudgetResolveTests(unittest.TestCase):
    def test_per_role_overrides_default(self) -> None:
        settings = {
            "runtime": {
                "agent_budgets": {
                    "default": "0.5",
                    "grilling_agent": "0.25",
                    "research_refiner_agent": "1.0",
                }
            }
        }
        self.assertEqual(resolve_agent_budget(settings, "grilling_agent"), "0.25")
        self.assertEqual(resolve_agent_budget(settings, "research_refiner_agent"), "1.0")
        # role with no entry uses default
        self.assertEqual(resolve_agent_budget(settings, "market_research_agent"), "0.5")

    def test_falls_back_to_live_backend_max_budget(self) -> None:
        settings = {
            "runtime": {
                "worker_backends": {
                    "claude_code_live": {"max_budget_usd": "0.5"}
                }
            }
        }
        self.assertEqual(resolve_agent_budget(settings, "grilling_agent"), "0.5")

    def test_falls_back_to_literal_default_when_nothing_configured(self) -> None:
        self.assertEqual(resolve_agent_budget({}, "grilling_agent"), "0.25")

    def test_accepts_numeric_values_and_coerces_to_string(self) -> None:
        """settings.json may declare budgets as either "0.5" or 0.5; both
        flow through to the Claude CLI as a string."""
        settings = {
            "runtime": {
                "agent_budgets": {
                    "grilling_agent": 0.5,  # number, not string
                    "default": 0.25,
                }
            }
        }
        self.assertEqual(resolve_agent_budget(settings, "grilling_agent"), "0.5")
        self.assertEqual(resolve_agent_budget(settings, "market_research_agent"), "0.25")


class AgentMaxRoundsResolveTests(unittest.TestCase):
    def test_per_role_overrides_default(self) -> None:
        settings = {
            "runtime": {
                "agent_max_rounds": {
                    "default": 8,
                    "grilling_agent": 5,
                    "research_refiner_agent": 20,
                }
            }
        }
        self.assertEqual(resolve_agent_max_rounds(settings, "grilling_agent"), 5)
        self.assertEqual(resolve_agent_max_rounds(settings, "research_refiner_agent"), 20)
        # role with no entry uses default
        self.assertEqual(resolve_agent_max_rounds(settings, "market_research_agent"), 8)

    def test_unlimited_string_returns_none(self) -> None:
        for token in ("unlimited", "UNLIMITED", "Unlimited", "none", "inf", "infinity"):
            with self.subTest(token=token):
                settings = {"runtime": {"agent_max_rounds": {"grilling_agent": token}}}
                self.assertIsNone(resolve_agent_max_rounds(settings, "grilling_agent"))

    def test_json_null_returns_none(self) -> None:
        settings = {"runtime": {"agent_max_rounds": {"grilling_agent": None}}}
        self.assertIsNone(resolve_agent_max_rounds(settings, "grilling_agent"))

    def test_string_integer_coerced(self) -> None:
        settings = {"runtime": {"agent_max_rounds": {"grilling_agent": "15"}}}
        self.assertEqual(resolve_agent_max_rounds(settings, "grilling_agent"), 15)

    def test_falls_back_to_caller_default(self) -> None:
        self.assertEqual(
            resolve_agent_max_rounds({}, "grilling_agent", fallback=12), 12
        )

    def test_invalid_shape_raises(self) -> None:
        bad_values = [
            {"value": 0, "label": "zero"},
            {"value": -1, "label": "negative"},
            {"value": True, "label": "bool"},
            {"value": "garbage", "label": "non-numeric string"},
        ]
        for case in bad_values:
            with self.subTest(label=case["label"]):
                settings = {
                    "runtime": {
                        "agent_max_rounds": {"grilling_agent": case["value"]}
                    }
                }
                with self.assertRaises(ConfigError):
                    resolve_agent_max_rounds(settings, "grilling_agent")

    def test_grilling_picks_up_explicit_model_from_settings(self) -> None:
        # Verify the wiring: grilling agent passes the resolved model to the CLI.
        captured = []

        def _runner(cmd, *, input, capture_output, text, timeout, check, env):
            captured.append(cmd)
            return _make_completed(
                _wrap_assistant_text(
                    json.dumps(
                        {
                            "action": "DONE",
                            "extracted": {
                                "root_goal_id": "rg_x",
                                "domain": "retrieval",
                                "node_type": "capability",
                                "claim_under_test": "x",
                                "mandatory_baselines": ["a"],
                                "success_criteria": ["b"],
                                "disproof_conditions": ["c"],
                                "goal_facets": [],
                                "taste_constraints": [],
                                "search_query_seed": "x",
                            },
                        }
                    )
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            run_grilling_session(
                REPO_ROOT,
                user_goal="x",
                run_dir=Path(tmp),
                billing_ack=True,
                execution_ack=True,
                command_runner=_runner,
                input_provider=lambda q: "",
                allowed_domains=["retrieval"],
            )
            # Whatever grilling_agent model is configured in settings.json
            # must be propagated as `--model <value>` to the CLI verbatim.
            from research_harness.config import load_settings, resolve_agent_model

            expected_model = resolve_agent_model(
                load_settings(REPO_ROOT), "grilling_agent"
            )
            cmd = captured[0]
            model_index = cmd.index("--model")
            self.assertEqual(cmd[model_index + 1], expected_model)


if __name__ == "__main__":
    unittest.main()
