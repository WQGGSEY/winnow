from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_harness.config import load_lessons, load_yaml
from research_harness.memory.failure_retrieval import (
    format_failure_summaries,
    retrieve_failure_summaries,
)
from research_harness.schemas.validator import validate_named_schema
from research_harness.workers.worker_task import (
    build_worker_task,
    validate_worker_task,
    write_worker_task,
)
from research_harness.workers.workspace import WorkspaceGuardError, ensure_path_inside


@dataclass(frozen=True)
class AuthPreflightResult:
    ok: bool
    mode: str
    reason: str | None = None
    details: dict[str, Any] | None = None


class ClaudeCodeInvoker:
    """Dry-run interface for future Claude Code subscription worker calls."""

    def __init__(
        self,
        workspace: Path,
        settings: dict[str, Any],
        repo_root: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.settings = settings
        self.repo_root = (repo_root or Path.cwd()).resolve()

    def auth_preflight(
        self,
        claude_path: str | None = None,
        probe_cli_status: bool = False,
    ) -> AuthPreflightResult:
        auth_policy = self.settings.get("runtime", {}).get("auth_policy", {})
        if auth_policy.get("require_no_anthropic_api_key") and os.environ.get("ANTHROPIC_API_KEY"):
            return AuthPreflightResult(
                ok=False,
                mode="api_key_would_take_precedence",
                reason="ANTHROPIC_API_KEY is set; subscription OAuth mode requires it to be unset.",
            )
        if probe_cli_status:
            return self._probe_claude_auth_status(claude_path or "claude")
        return AuthPreflightResult(ok=True, mode=auth_policy.get("mode", "unknown"))

    def _probe_claude_auth_status(self, claude_path: str) -> AuthPreflightResult:
        auth_policy = self.settings.get("runtime", {}).get("auth_policy", {})
        allowed_subscriptions = {
            str(item).lower()
            for item in auth_policy.get(
                "allowed_subscription_types",
                ["pro", "max", "team", "enterprise"],
            )
        }
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        try:
            completed = subprocess.run(
                [claude_path, "auth", "status", "--json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
        except FileNotFoundError:
            return AuthPreflightResult(
                ok=False,
                mode="claude_cli_missing",
                reason="claude CLI was not found for auth status probe.",
            )
        except subprocess.TimeoutExpired:
            return AuthPreflightResult(
                ok=False,
                mode="auth_status_timeout",
                reason="claude auth status timed out.",
            )

        raw_status = completed.stdout.strip() or completed.stderr.strip()
        try:
            status = json.loads(raw_status)
        except json.JSONDecodeError:
            return AuthPreflightResult(
                ok=False,
                mode="auth_status_unparseable",
                reason="claude auth status did not return JSON.",
            )

        details = {
            "logged_in": bool(status.get("loggedIn")),
            "auth_method": status.get("authMethod"),
            "api_provider": status.get("apiProvider"),
            "subscription_type": status.get("subscriptionType"),
        }
        if completed.returncode != 0 or not status.get("loggedIn"):
            return AuthPreflightResult(
                ok=False,
                mode="not_logged_in",
                reason="claude auth status reports no active login.",
                details=details,
            )
        if status.get("authMethod") != "claude.ai":
            return AuthPreflightResult(
                ok=False,
                mode="non_subscription_auth",
                reason="claude auth status is not using claude.ai subscription auth.",
                details=details,
            )
        subscription_type = str(status.get("subscriptionType") or "").lower()
        if subscription_type not in allowed_subscriptions:
            return AuthPreflightResult(
                ok=False,
                mode="unsupported_subscription_type",
                reason="claude auth status did not report an allowed subscription type.",
                details=details,
            )
        return AuthPreflightResult(
            ok=True,
            mode=auth_policy.get("mode", "subscription_oauth"),
            details=details,
        )

    def build_dry_run_invocation(
        self,
        node: dict[str, Any],
        worker_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime = node["runtime_profile"]
        role = runtime["worker_type"]
        if role == "runner_job":
            raise WorkspaceGuardError("runner_job is not a Claude Code worker role")

        self.workspace.mkdir(parents=True, exist_ok=True)
        prompt_path = self.workspace / "prompt.md"
        worker_task_path = self.workspace / "worker_task.json"
        expected_output_path = self.workspace / "worker_task_result.json"
        output_schema_path = (
            self.repo_root
            / "research_harness"
            / "schemas"
            / "worker_task_result.schema.json"
        )
        worker_task = worker_task or build_worker_task(node, self.workspace)
        validate_worker_task(node, worker_task, self.workspace)

        ensure_path_inside(prompt_path, self.workspace, "prompt_path")
        ensure_path_inside(worker_task_path, self.workspace, "worker_task_path")
        ensure_path_inside(expected_output_path, self.workspace, "expected_output_path")
        ensure_path_inside(output_schema_path, self.repo_root, "output_schema.path")

        envelope = {
            "backend": "claude_code_dry_run",
            "invocation_id": f"invoke_{node['id']}",
            "node_id": node["id"],
            "role": role,
            "workspace": str(self.workspace),
            "allowed_read_roots": [str(self.workspace)],
            "allowed_write_roots": [str(self.workspace)],
            "denied_write_roots": [
                str(self.repo_root),
                str(self.repo_root / "memory"),
                str(self.repo_root / "critics"),
            ],
            "permission_mode": "non_interactive_or_fail",
            "scope_policy": "no_self_expansion",
            "network": "disabled_by_default",
            "turn_budget": runtime.get("turn_budget") or self._default_turn_budget(role),
            "timeout_policy": runtime["timeout_policy"],
            "output_schema": {
                "name": "worker_task_result",
                "path": str(output_schema_path),
            },
            "expected_output_path": str(expected_output_path),
            "worker_task_path": str(worker_task_path),
            "environment_policy": {
                "unset": ["ANTHROPIC_API_KEY"],
                "secret_logging": "forbidden",
            },
            "prompt_path": str(prompt_path),
            "command_plan": {
                "executable": "claude",
                "args": [
                    "-p",
                    "--model",
                    str(
                        self.settings.get("runtime", {})
                        .get("worker_backends", {})
                        .get("claude_code_live", {})
                        .get("model", "sonnet")
                    ),
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    str(
                        self.settings.get("runtime", {})
                        .get("worker_backends", {})
                        .get("claude_code_live", {})
                        .get("tools", "")
                    ),
                    "--output-format",
                    "json",
                    "--input-format",
                    "text",
                    "--no-session-persistence",
                    "--max-budget-usd",
                    str(
                        self.settings.get("runtime", {})
                        .get("worker_backends", {})
                        .get("claude_code_live", {})
                        .get("max_budget_usd", "0.25")
                    ),
                ],
                "stdin_path": str(prompt_path),
                "executes_in_dry_run": False,
            },
        }
        self.validate_invocation_envelope(envelope)
        return envelope

    def build_worker_prompt(
        self,
        node: dict[str, Any],
        worker_task: dict[str, Any],
    ) -> str:
        contract = node["claim_contract"]
        baselines = "\n".join(f"- {item}" for item in contract["mandatory_baselines"])
        success = "\n".join(f"- {item}" for item in contract["success_criteria"])
        disproof = "\n".join(f"- {item}" for item in contract["disproof_conditions"])
        context_bundle = self._build_prompt_context_bundle(node)
        task_json = json.dumps(worker_task, indent=2, sort_keys=True)
        return (
            "# Claude Code Worker Contract\n\n"
            f"Role: {node['runtime_profile']['worker_type']}\n"
            f"Node: {node['id']}\n\n"
            "You are a bounded worker. Do not expand scope, choose new baselines, "
            "change the claim, mutate shared memory, or ask for interactive permission. "
            "For this live smoke, do not use tools. Treat this prompt as the complete "
            "context and return a schema-valid worker_task_result JSON on stdout only. "
            "You may report source_patch or observed_result only. You may describe "
            "branch suggestions, but you must not create branches. The harness will "
            "derive worker_report.json after validating this task result.\n\n"
            "## Claim Under Test\n"
            f"{contract['claim_under_test']}\n\n"
            "## Mandatory Baselines\n"
            f"{baselines}\n\n"
            "## Success Criteria\n"
            f"{success}\n\n"
            "## Disproof Conditions\n"
            f"{disproof}\n\n"
            "## Context Bundle\n"
            f"{context_bundle}\n\n"
            "## Worker Task JSON\n"
            "This is the binding task contract. Do not alter scope_locks.\n\n"
            "```json\n"
            f"{task_json}\n"
            "```\n\n"
            "## Output\n"
            "Return only raw JSON matching this shape. Preserve unknowns as null. "
            "Do not add facts that were not observed in this invocation.\n\n"
            "{\n"
            f"  \"task_id\": \"{worker_task['task_id']}\",\n"
            f"  \"node_id\": \"{node['id']}\",\n"
            "  \"status\": \"completed\",\n"
            "  \"output_kind\": \"observed_result\",\n"
            "  \"summary\": \"live smoke only; no experiment was executed\",\n"
            "  \"source_patch\": null,\n"
            "  \"observed_result\": {\n"
            "    \"claim_verdict_candidate\": \"not_evaluable\",\n"
            "    \"metrics\": {\"live_smoke_json_contract\": 1},\n"
            "    \"baselines\": {},\n"
            "    \"disproof_conditions_hit\": [],\n"
            "    \"artifacts\": [],\n"
            "    \"unexpected_observations\": []\n"
            "  },\n"
            "  \"branch_suggestions\": [],\n"
            "  \"scope_check\": {\n"
            "    \"claim_changed\": false,\n"
            "    \"baselines_changed\": false,\n"
            "    \"shared_memory_write_attempted\": false,\n"
            "    \"branch_created\": false,\n"
            "    \"files_written_outside_workspace\": false\n"
            "  },\n"
            "}\n"
        )

    def _build_prompt_context_bundle(self, node: dict[str, Any]) -> str:
        return "\n".join(
            [
                "### Active Lessons",
                self._active_lessons_context(),
                "",
                "### Baseline Dossier Summary",
                self._baseline_context(node),
                "",
                "### Failure Retrieval Index",
                self._failure_context(node),
            ]
        )

    def _active_lessons_context(self) -> str:
        lessons = load_lessons(self.repo_root).get("active_lessons", [])
        if not lessons:
            return "- none"
        return "\n".join(
            f"- {lesson.get('id')}: {lesson.get('text')}"
            for lesson in lessons
        )

    def _baseline_context(self, node: dict[str, Any]) -> str:
        lines: list[str] = []
        for ref in node.get("baseline_refs", []):
            dossier_id = ref.get("baseline_dossier_id")
            if not dossier_id:
                continue
            dossier_path = (
                self.repo_root / "memory" / "baseline_dossiers" / f"{dossier_id}.yaml"
            )
            dossier = load_yaml(dossier_path)
            selected = dossier.get("selected", {})
            lines.append(
                "- dossier "
                f"{dossier_id}: selected={selected.get('candidate_id')} "
                f"role={selected.get('role')} reason={selected.get('one_paragraph_reason')}"
            )
            for candidate in dossier.get("candidates_index", []):
                tags = ", ".join(str(tag) for tag in candidate.get("reason_tags", []))
                lines.append(
                    "  - candidate "
                    f"{candidate.get('id')}: decision={candidate.get('decision')} "
                    f"method={candidate.get('method')} tags=[{tags}]"
                )
        return "\n".join(lines) if lines else "- none"

    def _failure_context(self, node: dict[str, Any]) -> str:
        failure_index_path = self.repo_root / "memory" / "failures" / "index.yaml"
        failures = load_yaml(failure_index_path)
        retrieval = node.get("failure_retrieval", {})
        query_tag_list = [str(tag) for tag in retrieval.get("query_tags", [])]
        query_tags = ", ".join(query_tag_list)
        selected_fail_files = retrieval.get("selected_fail_files", [])
        top_k = int(
            self.settings.get("memory", {}).get("failure_retrieval_top_k", 5)
        )
        summaries = retrieve_failure_summaries(
            self.repo_root,
            query_tags=[node.get("domain", ""), *query_tag_list],
            selected_fail_files=selected_fail_files,
            top_k=top_k,
        )
        lines = [f"- query_tags=[{query_tags}]", "- categories:"]
        for category, spec in failures.get("categories", {}).items():
            file_count = len(spec.get("files", []) or [])
            lines.append(
                f"  - {category}: files={file_count}; {spec.get('description')}"
            )
        lines.append(format_failure_summaries(summaries))
        return "\n".join(lines)

    def write_dry_run_artifacts(
        self,
        node: dict[str, Any],
        envelope_path: Path | None = None,
        worker_task: dict[str, Any] | None = None,
        experiment_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if worker_task is None:
            worker_task = write_worker_task(
                node,
                self.workspace,
                experiment_plan=experiment_plan,
            )
        else:
            validate_worker_task(node, worker_task, self.workspace)
            worker_task_path = self.workspace / "worker_task.json"
            worker_task_path.parent.mkdir(parents=True, exist_ok=True)
            worker_task_path.write_text(
                json.dumps(worker_task, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        envelope = self.build_dry_run_invocation(node, worker_task=worker_task)
        prompt_path = Path(envelope["prompt_path"])
        prompt_path.write_text(
            self.build_worker_prompt(node, worker_task),
            encoding="utf-8",
        )

        envelope_path = envelope_path or self.workspace / "invocation_envelope.json"
        ensure_path_inside(envelope_path, self.workspace, "invocation_envelope")
        envelope_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
        return envelope

    def validate_invocation_envelope(self, envelope: dict[str, Any]) -> None:
        validate_named_schema("invocation_envelope", envelope)
        workspace = Path(envelope["workspace"]).resolve()
        if workspace == self.repo_root:
            raise WorkspaceGuardError("Claude Code worker workspace cannot be repo root")
        for write_root in envelope["allowed_write_roots"]:
            ensure_path_inside(Path(write_root), workspace, "allowed_write_roots")
        ensure_path_inside(Path(envelope["prompt_path"]), workspace, "prompt_path")
        ensure_path_inside(
            Path(envelope["worker_task_path"]),
            workspace,
            "worker_task_path",
        )
        ensure_path_inside(
            Path(envelope["expected_output_path"]),
            workspace,
            "expected_output_path",
        )
        output_schema = Path(envelope["output_schema"]["path"])
        if output_schema.name != "worker_task_result.schema.json":
            raise WorkspaceGuardError(
                "Claude Code worker must emit worker_task_result schema"
            )
        ensure_path_inside(output_schema, self.repo_root, "output_schema.path")

    def _default_turn_budget(self, role: str) -> int:
        return int(
            self.settings.get("runtime", {})
            .get("worker_turn_budgets", {})
            .get(role, 1)
        )
