from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from research_harness.schemas.validator import (
    SchemaValidationError,
    validate_named_schema,
)
from research_harness.workers.workspace import ensure_path_inside


DEFAULT_TEMPLATES_DIRNAME = "experiment_plan_templates"
FALLBACK_TEMPLATE_ID = "_fallback_demo"
PLAN_METADATA_FILENAME = "plan.json"
SRC_DIRNAME = "src"
ALLOWED_SOURCE_SUFFIXES = {
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".md",
    ".toml",
    ".cfg",
    ".sh",
}
MAX_SOURCE_FILE_BYTES = 200_000
MAX_SOURCE_TREE_BYTES = 5_000_000
IGNORED_DIR_NAMES = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".venv"}


class ExperimentPlanError(ValueError):
    """Raised when a plan attempts to drift away from the node contract."""


def template_directories(
    settings: dict[str, Any] | None,
) -> list[str]:
    """Read the operator-configured list of template directories from settings.

    Settings shape:
      "experiment_plan_templates": {"directories": ["experiment_plan_templates", ...]}
    Defaults to the bundled directory if unset.
    """

    if not settings:
        return [DEFAULT_TEMPLATES_DIRNAME]
    block = settings.get("experiment_plan_templates", {}) or {}
    directories = block.get("directories") or [DEFAULT_TEMPLATES_DIRNAME]
    return [str(item) for item in directories]


def list_available_domains(
    repo_root: Path,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    """List domain ids the router will accept.

    A directory under any configured template directory counts as a domain
    only if it contains both ``plan.json`` and a non-empty ``src/`` tree.
    """

    domains: set[str] = set()
    for dirname in template_directories(settings):
        dir_path = repo_root / dirname
        if not dir_path.is_dir():
            continue
        for child in dir_path.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("_") or child.name in IGNORED_DIR_NAMES:
                continue
            if not _is_safe_module_name(child.name):
                continue
            if (child / PLAN_METADATA_FILENAME).is_file() and (child / SRC_DIRNAME).is_dir():
                domains.add(child.name)
    return sorted(domains)


def build_experiment_plan_for_node(
    repo_root: Path,
    node: dict[str, Any],
    run_dir: Path,
    settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Route to the Professor-generated thread template, falling back to a
    legacy domain template only when the LLM orchestrator is disabled.

    The thread-specific Professor lib lives at::

        <run_dir>/../professor_templates/_lib/   ← shared reusable modules
        <run_dir>/../professor_templates/<node_id>/  ← per-node experiment.py

    Resolution order:
      1. If a per-node template dir already exists with plan.json + src,
         materialize from it (cached output from a previous design call).
      2. If the LLM orchestrator is enabled, call Professor.design_experiment,
         write its files to disk, then materialize.
      3. Else (legacy / orchestrator disabled): fall back to the old
         experiment_plan_templates/<domain>/ lookup, and finally demo plan.
    """

    template_root = _professor_template_root(run_dir)
    node_dir = template_root / _safe_node_dirname(node["id"])
    if (node_dir / PLAN_METADATA_FILENAME).is_file() and (node_dir / SRC_DIRNAME).is_dir():
        return _build_plan_from_professor_template(
            template_dir=node_dir,
            template_root=template_root,
            node=node,
            run_dir=run_dir,
        )

    llm_cfg = (settings or {}).get("runtime", {}).get("llm_orchestrator", {}) or {}
    if llm_cfg.get("enabled", False):
        _materialize_professor_template(
            repo_root=repo_root,
            template_root=template_root,
            node_dir=node_dir,
            node=node,
            settings=settings or {},
        )
        return _build_plan_from_professor_template(
            template_dir=node_dir,
            template_root=template_root,
            node=node,
            run_dir=run_dir,
        )

    # Legacy fallback: keep old domain-template routing intact for callers
    # who haven't enabled the LLM orchestrator yet.
    domain = str(node.get("domain") or "").strip()
    if domain and _is_safe_module_name(domain):
        for dirname in template_directories(settings):
            domain_dir = repo_root / dirname / domain
            plan_path = domain_dir / PLAN_METADATA_FILENAME
            src_dir = domain_dir / SRC_DIRNAME
            if plan_path.is_file() and src_dir.is_dir():
                plan = _build_plan_from_directory(
                    domain_dir=domain_dir,
                    domain=domain,
                    node=node,
                    run_dir=run_dir,
                )
                try:
                    validate_named_schema("experiment_plan", plan)
                except SchemaValidationError as exc:
                    raise ExperimentPlanError(
                        f"template {domain!r} produced schema-invalid plan: {exc}"
                    ) from exc
                return plan, domain
    return build_demo_experiment_plan(node, run_dir), FALLBACK_TEMPLATE_ID


def _professor_template_root(run_dir: Path) -> Path:
    """The Professor's per-thread template directory.

    `run_dir` here is the production/tree run dir (e.g.
    `runs/threads/<tid>/production/tree`); the templates live in a sibling
    directory `professor_templates/` under the production phase root so
    they survive across retries.
    """
    return run_dir.resolve().parent / "professor_templates"


def _safe_node_dirname(node_id: str) -> str:
    """Coerce a node id into a filesystem-safe directory name."""
    safe = "".join(c if c.isalnum() or c in {"_", "-"} else "_" for c in node_id)
    return safe or "node"


def _materialize_professor_template(
    *,
    repo_root: Path,
    template_root: Path,
    node_dir: Path,
    node: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    """Call Professor.design_experiment and write its output to disk.

    Files prefixed with `_lib/` go to the thread-wide shared lib; everything
    else lands in `node_dir/`. Re-emitted shared files are skipped silently
    (idempotent: later directions that emit the same shared helper do not
    clobber an earlier version).
    """
    from research_harness.orchestrator.llm_orchestrator import (
        MockLLMClient,
        Professor,
        build_llm_client,
    )
    from research_harness.orchestrator.llm_orchestrator.mock_handlers import (
        register_default_mock_handlers,
    )

    client = build_llm_client(settings, role="professor")
    if isinstance(client, MockLLMClient):
        register_default_mock_handlers(client)
    professor = Professor(client)
    lib_dir = template_root / "_lib"
    shared_modules: list[str] = []
    if lib_dir.is_dir():
        for p in lib_dir.rglob("*.py"):
            shared_modules.append(p.relative_to(lib_dir).as_posix())
    plan_meta, _dialog = professor.design_experiment(
        node=node,
        shared_lib_modules=shared_modules,
        shared_lib_module_name="_lib" if shared_modules else None,
    )

    node_dir.mkdir(parents=True, exist_ok=True)
    src_dir = node_dir / SRC_DIRNAME
    src_dir.mkdir(parents=True, exist_ok=True)

    plan_metadata_payload = {
        k: v for k, v in plan_meta.items() if k != "source_files"
    }
    # Drop legacy 'inputs' if it leaked in — user_experiment_plan_metadata
    # already has it as optional and we want to keep this clean.
    (node_dir / PLAN_METADATA_FILENAME).write_text(
        __import_json().dumps(plan_metadata_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    for sf in plan_meta.get("source_files") or []:
        rel = sf.get("path", "")
        content = sf.get("content", "")
        if not rel or not content:
            continue
        if rel.startswith("_lib/"):
            target = template_root / rel
        else:
            target = node_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and rel.startswith("_lib/"):
            # Idempotent: never overwrite an existing shared lib file.
            continue
        target.write_text(content, encoding="utf-8")


def __import_json():
    import json as _json
    return _json


def _build_plan_from_professor_template(
    *,
    template_dir: Path,
    template_root: Path,
    node: dict[str, Any],
    run_dir: Path,
) -> tuple[dict[str, Any], str]:
    """Materialize a Professor-generated template into an experiment_plan."""
    metadata = _load_user_metadata(
        template_dir / PLAN_METADATA_FILENAME, node["id"]
    )
    workspace = (run_dir / "nodes" / node["id"] / "workspace").resolve()

    # Collect source files from BOTH the per-node dir and the shared _lib.
    source_files: list[dict[str, str]] = []
    seen: set[str] = set()
    node_src = template_dir / SRC_DIRNAME
    if node_src.is_dir():
        for entry in _collect_source_files(node_src, node["id"]):
            source_files.append(entry)
            seen.add(entry["path"])
    lib_dir = template_root / "_lib"
    if lib_dir.is_dir():
        for entry in _collect_lib_files(lib_dir):
            if entry["path"] in seen:
                continue
            source_files.append(entry)
            seen.add(entry["path"])

    plan_id_suffix = str(metadata.get("plan_id_suffix") or metadata["task_class"])
    contract = node["claim_contract"]
    plan: dict[str, Any] = {
        "plan_id": f"plan_{node['id']}_{plan_id_suffix}",
        "node_id": node["id"],
        "claim_under_test": contract["claim_under_test"],
        "objective": metadata["objective"],
        "task_class": metadata["task_class"],
        "workspace": str(workspace),
        "source_files": source_files,
        "entrypoint": metadata["entrypoint"],
        "resources": metadata["resources"],
        "inputs": metadata.get("inputs") or {"datasets": [], "snapshots": []},
        "expected_outputs": metadata["expected_outputs"],
        "baseline_evidence_requirements": metadata["baseline_evidence_requirements"],
        "mandatory_baselines": list(contract["mandatory_baselines"]),
        "success_criteria": list(contract["success_criteria"]),
        "disproof_conditions": list(contract["disproof_conditions"]),
        "guardrails": metadata.get("guardrails") or {
            "allowed_write_roots": ["workspace"],
            "forbidden_actions": [
                "scope_expansion",
                "baseline_changes",
                "shared_memory_write",
                "critic_routing_changes",
                "publication_gate_changes",
            ],
            "scope_policy": "orchestrator_owned_search_policy",
        },
        "failure_index_hints": metadata.get("failure_index_hints") or {
            "domain_tags": ["professor_generated"],
            "method_tags": ["professor_template"],
            "risk_tags": list(node["failure_retrieval"].get("query_tags") or []),
        },
        "reproducibility": metadata.get("reproducibility") or {
            "seed": 0,
            "code_snapshot": f"professor_templates/{node['id']}",
            "data_snapshot": "professor_generated",
        },
    }
    _validate_entrypoint_args_resolve_to_source(
        plan["entrypoint"], source_files, node["id"]
    )
    try:
        validate_named_schema("experiment_plan", plan)
    except SchemaValidationError as exc:
        raise ExperimentPlanError(
            f"Professor template for {node['id']} produced schema-invalid plan: {exc}"
        ) from exc
    return plan, "professor_generated"


def _collect_lib_files(lib_dir: Path) -> list[dict[str, str]]:
    """Collect files under the thread-wide `_lib/` and prefix paths so they
    materialize at `<workspace>/_lib/...` next to `src/`."""
    files: list[dict[str, str]] = []
    lib_root = lib_dir.resolve()
    for p in sorted(lib_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in ALLOWED_SOURCE_SUFFIXES:
            continue
        relative = p.resolve().relative_to(lib_root)
        if any(part in IGNORED_DIR_NAMES for part in relative.parts):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not content:
            content = "\n"
        files.append(
            {
                "path": (Path("_lib") / relative).as_posix(),
                "purpose": "Professor-generated shared lib module",
                "content": content,
            }
        )
    return files


def _is_safe_module_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _build_plan_from_directory(
    *,
    domain_dir: Path,
    domain: str,
    node: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    metadata = _load_user_metadata(domain_dir / PLAN_METADATA_FILENAME, domain)
    source_files = _collect_source_files(domain_dir / SRC_DIRNAME, domain)
    workspace = (run_dir / "nodes" / node["id"] / "workspace").resolve()
    plan_id_suffix = str(metadata.get("plan_id_suffix") or metadata["task_class"])
    contract = node["claim_contract"]

    plan: dict[str, Any] = {
        "plan_id": f"plan_{node['id']}_{plan_id_suffix}",
        "node_id": node["id"],
        "claim_under_test": contract["claim_under_test"],
        "objective": metadata["objective"],
        "task_class": metadata["task_class"],
        "workspace": str(workspace),
        "source_files": source_files,
        "entrypoint": metadata["entrypoint"],
        "resources": metadata["resources"],
        "inputs": metadata.get("inputs") or {"datasets": [], "snapshots": []},
        "expected_outputs": metadata["expected_outputs"],
        "baseline_evidence_requirements": metadata["baseline_evidence_requirements"],
        "mandatory_baselines": list(contract["mandatory_baselines"]),
        "success_criteria": list(contract["success_criteria"]),
        "disproof_conditions": list(contract["disproof_conditions"]),
        "guardrails": metadata.get("guardrails")
        or {
            "allowed_write_roots": ["workspace"],
            "forbidden_actions": [
                "scope_expansion",
                "baseline_changes",
                "shared_memory_write",
                "critic_routing_changes",
                "publication_gate_changes",
            ],
            "scope_policy": "orchestrator_owned_search_policy",
        },
        "failure_index_hints": metadata.get("failure_index_hints")
        or {
            "domain_tags": [domain],
            "method_tags": ["user_template"],
            "risk_tags": list(node["failure_retrieval"].get("query_tags") or []),
        },
        "reproducibility": metadata.get("reproducibility")
        or {
            "seed": 0,
            "code_snapshot": f"experiment_plan_templates/{domain}",
            "data_snapshot": "user_supplied",
        },
    }

    _validate_entrypoint_args_resolve_to_source(plan["entrypoint"], source_files, domain)
    return plan


def _load_user_metadata(path: Path, domain: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExperimentPlanError(f"template {domain!r} cannot read plan.json: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExperimentPlanError(
            f"template {domain!r} plan.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ExperimentPlanError(f"template {domain!r} plan.json must be an object")
    try:
        validate_named_schema("user_experiment_plan_metadata", data)
    except SchemaValidationError as exc:
        raise ExperimentPlanError(
            f"template {domain!r} plan.json failed schema validation: {exc}"
        ) from exc
    return data


def _collect_source_files(src_dir: Path, domain: str) -> list[dict[str, str]]:
    if not src_dir.is_dir():
        raise ExperimentPlanError(f"template {domain!r} is missing the src/ directory")
    files: list[dict[str, str]] = []
    total_bytes = 0
    src_root = src_dir.resolve()
    for path in sorted(src_dir.rglob("*")):
        if path.is_dir():
            if path.name in IGNORED_DIR_NAMES:
                # rglob still walks into it; we skip its children below
                continue
            continue
        # Skip files that live inside an ignored directory.
        relative_to_root = path.resolve().relative_to(src_root)
        if any(part in IGNORED_DIR_NAMES for part in relative_to_root.parts):
            continue
        if path.suffix not in ALLOWED_SOURCE_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ExperimentPlanError(
                f"template {domain!r} cannot stat {path}: {exc}"
            ) from exc
        if size > MAX_SOURCE_FILE_BYTES:
            raise ExperimentPlanError(
                f"template {domain!r} source file too large (>{MAX_SOURCE_FILE_BYTES} bytes): "
                f"{relative_to_root}"
            )
        total_bytes += size
        if total_bytes > MAX_SOURCE_TREE_BYTES:
            raise ExperimentPlanError(
                f"template {domain!r} src/ tree exceeds {MAX_SOURCE_TREE_BYTES} bytes"
            )
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ExperimentPlanError(
                f"template {domain!r} source file is not UTF-8 text: {relative_to_root}: {exc}"
            ) from exc
        if not content:
            # Schema requires non-empty content; empty __init__.py files etc.
            # get a single-byte placeholder so the materialized file is still
            # functionally empty when written back to the workspace.
            content = "\n"
        workspace_path = (Path(SRC_DIRNAME) / relative_to_root).as_posix()
        files.append(
            {
                "path": workspace_path,
                "purpose": f"user-supplied source from template {domain}",
                "content": content,
            }
        )
    if not files:
        raise ExperimentPlanError(
            f"template {domain!r} src/ tree contains no allowed source files"
        )
    return files


def _validate_entrypoint_args_resolve_to_source(
    entrypoint: dict[str, Any],
    source_files: list[dict[str, str]],
    domain: str,
) -> None:
    """Best-effort check that the first positional arg matches a source file.

    Many entrypoints look like ``python src/experiment.py``; ensure the file
    referenced exists in the materialized tree so we fail before the runner
    tries to execute a missing path.
    """

    args = list(entrypoint.get("args") or [])
    if not args:
        return
    candidate = args[0]
    if not candidate or candidate.startswith("-"):
        return
    if any(file["path"] == candidate for file in source_files):
        return
    raise ExperimentPlanError(
        f"template {domain!r} entrypoint.args[0]={candidate!r} does not match "
        "any file collected from src/. Use a workspace-relative path like "
        "'src/experiment.py'."
    )


def build_demo_experiment_plan(node: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    workspace = run_dir / "nodes" / node["id"] / "workspace"
    return {
        "plan_id": f"plan_{node['id']}_smoke",
        "node_id": node["id"],
        "claim_under_test": node["claim_contract"]["claim_under_test"],
        "objective": (
            "Generate a deterministic smoke experiment that measures whether "
            "bounded runner evidence can support the node claim without giving "
            "the worker search-policy authority."
        ),
        "task_class": "smoke_test",
        "workspace": str(workspace.resolve()),
        "source_files": [
            {
                "path": "experiment.py",
                "purpose": "Write deterministic metrics for runner evidence ingestion.",
                "content": _demo_experiment_source(),
            }
        ],
        "entrypoint": {
            "command": [sys.executable],
            "args": ["experiment.py"],
        },
        "resources": {
            "timeout_sec": 60,
            "gpu": None,
            "cpu": 1,
            "memory_gb": 1,
        },
        "inputs": {
            "datasets": [],
            "snapshots": [],
        },
        "expected_outputs": {
            "metrics_files": ["artifacts/metrics.json"],
            "logs": ["artifacts/run.log"],
            "artifact_dirs": ["artifacts/"],
        },
        "baseline_evidence_requirements": [
            {
                "role": "current_best_known",
                "metric_key": "bounded_worker_success_rate",
                "baseline_key": "current_best_known",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            },
            {
                "role": "naive",
                "metric_key": "bounded_worker_success_rate",
                "baseline_key": "naive_direct_port",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            },
            {
                "role": "random_or_null",
                "metric_key": "bounded_worker_success_rate",
                "baseline_key": "random_or_null",
                "operator": "greater_than",
                "margin": 0,
                "required": True,
            },
        ],
        "mandatory_baselines": list(node["claim_contract"]["mandatory_baselines"]),
        "success_criteria": list(node["claim_contract"]["success_criteria"]),
        "disproof_conditions": list(node["claim_contract"]["disproof_conditions"]),
        "guardrails": {
            "allowed_write_roots": ["workspace"],
            "forbidden_actions": [
                "scope_expansion",
                "baseline_changes",
                "shared_memory_write",
                "critic_routing_changes",
                "publication_gate_changes",
            ],
            "scope_policy": "orchestrator_owned_search_policy",
        },
        "failure_index_hints": {
            "domain_tags": [node["domain"]],
            "method_tags": [
                "experiment_plan",
                "bounded_worker",
                "deterministic_runner",
            ],
            "risk_tags": list(node["failure_retrieval"]["query_tags"]),
        },
        "reproducibility": {
            "seed": 0,
            "code_snapshot": "local_pre_live_scaffold",
            "data_snapshot": "none",
        },
    }


def validate_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    run_dir: Path,
) -> None:
    validate_named_schema("experiment_plan", experiment_plan)
    if experiment_plan["node_id"] != node["id"]:
        raise ExperimentPlanError("experiment plan node_id does not match node")

    contract = node["claim_contract"]
    expected_pairs = {
        "claim_under_test": contract["claim_under_test"],
        "mandatory_baselines": contract["mandatory_baselines"],
        "success_criteria": contract["success_criteria"],
        "disproof_conditions": contract["disproof_conditions"],
    }
    for key, expected in expected_pairs.items():
        if experiment_plan[key] != expected:
            raise ExperimentPlanError(
                f"experiment plan cannot override node claim_contract.{key}"
            )

    workspace = Path(experiment_plan["workspace"]).resolve()
    ensure_path_inside(workspace, run_dir.resolve(), "experiment plan workspace")

    seen_source_paths: set[str] = set()
    for source_file in experiment_plan["source_files"]:
        source_path = _relative_workspace_path(source_file["path"], "source_files")
        normalized = source_path.as_posix()
        if normalized in seen_source_paths:
            raise ExperimentPlanError(f"duplicate source file path: {normalized}")
        seen_source_paths.add(normalized)
        ensure_path_inside(workspace / source_path, workspace, "source_files")

    if contract.get("data_source_snapshot_id"):
        source_blob = "\n".join(
            source_file["content"] for source_file in experiment_plan["source_files"]
        )
        missing_references = []
        if "RESEARCH_HARNESS_INPUT_MANIFEST" not in source_blob:
            missing_references.append("RESEARCH_HARNESS_INPUT_MANIFEST")
        if not (
            re.search(r"(['\"])primary_dataset\1", source_blob)
            and re.search(r"(['\"])relative_path\1", source_blob)
        ):
            missing_references.append("primary_dataset.relative_path")
        if missing_references:
            raise ExperimentPlanError(
                "registered-adapter experiment source must reference "
                + " and ".join(missing_references)
            )

    for key in ("metrics_files", "logs", "artifact_dirs"):
        for raw_path in experiment_plan["expected_outputs"].get(key, []):
            output_path = _relative_workspace_path(raw_path, key)
            ensure_path_inside(workspace / output_path, workspace, key)

    if "workspace" not in experiment_plan["guardrails"]["allowed_write_roots"]:
        raise ExperimentPlanError("experiment plan must restrict writes to workspace")

    required_roles = {"current_best_known", "naive", "random_or_null"}
    planned_roles = {
        requirement["role"]
        for requirement in experiment_plan["baseline_evidence_requirements"]
        if requirement["required"]
    }
    missing_roles = sorted(required_roles - planned_roles)
    if missing_roles:
        raise ExperimentPlanError(
            "experiment plan missing required baseline evidence roles: "
            + ", ".join(missing_roles)
        )


def materialize_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    run_dir: Path,
) -> list[str]:
    validate_experiment_plan(node, experiment_plan, run_dir)
    workspace = Path(experiment_plan["workspace"]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for source_file in experiment_plan["source_files"]:
        relative_path = _relative_workspace_path(source_file["path"], "source_files")
        target = (workspace / relative_path).resolve()
        ensure_path_inside(target, workspace, "source_files")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source_file["content"], encoding="utf-8")
        written.append(relative_path.as_posix())
    return written


def build_job_manifest_from_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    source_files = materialize_experiment_plan(node, experiment_plan, run_dir)
    return derive_job_manifest_from_experiment_plan(
        node,
        experiment_plan,
        source_files=source_files,
    )


def derive_job_manifest_from_experiment_plan(
    node: dict[str, Any],
    experiment_plan: dict[str, Any],
    *,
    source_files: list[str] | None = None,
) -> dict[str, Any]:
    """Derive the runner contract without materializing or changing files."""

    normalized_sources = source_files or [
        _relative_workspace_path(source["path"], "source_files").as_posix()
        for source in experiment_plan["source_files"]
    ]
    return {
        "job_id": f"job_{node['id']}_{experiment_plan['task_class']}",
        "experiment_plan_id": experiment_plan["plan_id"],
        "node_id": node["id"],
        "task_class": experiment_plan["task_class"],
        "workspace": experiment_plan["workspace"],
        "source_files": normalized_sources,
        "entrypoint": experiment_plan["entrypoint"],
        "resources": experiment_plan["resources"],
        "inputs": experiment_plan["inputs"],
        "outputs": experiment_plan["expected_outputs"],
        "baseline_evidence_requirements": experiment_plan[
            "baseline_evidence_requirements"
        ],
        "claim_contract": node["claim_contract"],
        "failure_index_hints": experiment_plan["failure_index_hints"],
        "reproducibility": experiment_plan["reproducibility"],
    }


def _relative_workspace_path(raw_path: str, field_name: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ExperimentPlanError(f"{field_name} must use relative workspace paths")
    return path


def _demo_experiment_source() -> str:
    return "\n".join(
        [
            "import json",
            "from pathlib import Path",
            "",
            "artifacts = Path('artifacts')",
            "artifacts.mkdir(exist_ok=True)",
            "payload = {",
            "    'metrics': {",
            "        'bounded_worker_success_rate': 0.92,",
            "        'schema_validity': 1.0,",
            "    },",
            "    'baselines': {",
            "        'current_best_known': 0.80,",
            "        'naive_direct_port': 0.45,",
            "        'random_or_null': 0.05,",
            "    },",
            "    'claim_verdict_candidate': 'supported',",
            "    'disproof_conditions_hit': [],",
            "    'unexpected_observations': [",
            "        {",
            "            'observation': 'The runtime envelope is the main differentiator from a direct API port.',",
            "            'evidence': 'Naive direct port baseline lacks scope, permission, and output-schema controls.',",
            "            'suggested_branch_type': 'validity',",
            "            'scope_relation': 'directly_explains_success',",
            "        }",
            "    ],",
            "}",
            "(artifacts / 'metrics.json').write_text(json.dumps(payload, indent=2) + '\\n')",
            "print('runner smoke metrics written')",
            "",
        ]
    )
