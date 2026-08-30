#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

command -v codex >/dev/null
codex login status >/dev/null

pytest_cmd=(python -m pytest)
if [[ -x "$repo_root/venv/bin/pytest" ]]; then
  pytest_cmd=("$repo_root/venv/bin/pytest")
fi

"${pytest_cmd[@]}" -q \
  tests/test_codex_cli_adapter.py \
  tests/test_grilling.py \
  tests/test_connector_abstraction.py \
  tests/test_connector_reading_prune1.py \
  tests/test_connector_reduction_market.py \
  tests/test_connector_orchestrator.py \
  tests/test_thread_supervisor.py \
  tests/test_supervisor_stall.py \
  tests/test_invocation_envelope.py \
  tests/test_codex_stdout_ingest.py \
  tests/test_live_gate.py \
  tests/test_live_smoke_runner.py

if [[ "${RESEARCH_HARNESS_VERIFY_CODEX_LIVE:-0}" == "1" ]]; then
  model="${RESEARCH_HARNESS_CODEX_MODEL:-gpt-5.6-sol}"
  MODEL="$model" python - <<'PY'
import os

from research_harness.adapters.codex_cli import CodexCliAdapter
from research_harness.agent_runtime import AgentPrompt, CompletionRequest

result = CodexCliAdapter().complete(
    CompletionRequest(
        prompt=AgentPrompt(
            instructions="Reply with exactly CODEX_ADAPTER_OK.",
            input="Run the ephemeral adapter verification probe.",
        ),
        model=os.environ["MODEL"],
        timeout_seconds=120,
        label="live adapter probe",
    )
)
if result.text.strip() != "CODEX_ADAPTER_OK":
    raise SystemExit(f"unexpected live probe output: {result.text!r}")
print("live probe passed")
PY
fi
