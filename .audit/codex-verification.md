# Codex adapter verification evidence

Recorded from the Codex workspace transcript on 2026-08-30. Commands ran from
`/home/hsj68/research_harness` against the working tree described by
`.audit/codex-adapter.tsv`.

## Runtime and authentication

```text
$ codex --version
codex-cli 0.150.1
$ codex login status
Logged in using ChatGPT
```

## Live one-shot completion (initial migration probe)

Command:

```text
RESEARCH_HARNESS_VERIFY_CODEX_LIVE=1 RESEARCH_HARNESS_CODEX_MODEL=gpt-5.6-sol bash scripts/verify_codex_adapter.sh
```

Observed result:

```text
121 passed, 5 subtests passed in 4.08s
live probe passed
```

This earlier probe established the live authentication path. The current
verification counts are recorded under "Final local verification" below. The
script calls `CodexCliAdapter.complete()` and requires the final parsed agent
message to equal `CODEX_ADAPTER_OK`.

## Live schema-constrained completion

`CodexCliAdapter.complete()` ran with
`.audit/codex-probe.schema.json` as `CompletionRequest.output_schema`.

Observed parsed result:

```text
{'payload': {'ok': True, 'runtime': 'codex'}, 'usage': {'input_tokens': 20613, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0, 'output_tokens': 20, 'reasoning_output_tokens': 0}}
```

The persisted schema output is `.audit/codex-probe-output.json`.

## Live per-invocation MCP

`CodexCliAdapter.start_session()` ran with `ResearchHarnessMcp.from_settings()`.
The prompt required one read-only
`research_harness.get_research_state(thread_id="thread_codex_adapter_probe_missing")`
call and an exact final response.

Observed result:

```text
{'exit_code': 0, 'tool_events': ['research_harness'], 'final': 'MCP_ADAPTER_OK'}
```

No global Codex MCP server was registered for this probe. The adapter supplied
the command, arguments, and environment through per-invocation configuration.

```text
$ codex mcp list
No MCP servers configured yet. Try `codex mcp add my-tool -- my-command`.
```

## Final local verification

```text
$ PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider
788 passed, 17 subtests passed in 20.23s
$ bash scripts/verify_codex_adapter.sh
Logged in using ChatGPT
127 passed, 5 subtests passed in 5.11s
$ git diff --check
<no output, exit 0>
```

The current suite includes the registered-adapter snapshot and runtime binding
pipeline, baseline-contract recovery, terminal lifecycle synchronization, and
the exact `primary_dataset.relative_path` input contract. Claude stdout
wire-format cases were removed with that adapter; Codex JSONL, live gate,
arbitrary-node, path-binding, billing, timeout, and schema-output cases protect
the active runtime invariants.
