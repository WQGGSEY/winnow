"""Run isolated scientific reviewers and replay their persisted evidence."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Protocol

from research_harness.adapters.codex_cli import CodexCliAdapter
from research_harness.agent_runtime import AgentPrompt, CompletionRequest, CompletionResult
from research_harness.publishing.integrity import json_digest
from research_harness.publishing.scientific_review import assess_readiness
from research_harness.schemas.validator import validate_schema


class ScientificReviewRunError(ValueError):
    pass


class CompletionTransport(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResult: ...


_REVIEWERS = (
    ("scientific-reviewer-methods", "Focus on empirical support, argument completeness, reproducibility, and limitations."),
    ("scientific-reviewer-literature", "Focus on importance, closest work, claimed differences, and scope."),
)


def _bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScientificReviewRunError(f"unreadable {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScientificReviewRunError(f"{label} must contain an object")
    return value


def _response_schema(repo_root: Path) -> Path:
    return repo_root / "research_harness" / "schemas" / "scientific_review_response.schema.json"


def _prompt(paper: str, ledger: dict[str, Any], emphasis: str) -> AgentPrompt:
    return AgentPrompt(
        instructions=(
            "Act as an independent scientific reviewer. Use only the supplied complete manuscript "
            "and evidence ledger; do not inspect files or use tools. Assess all five categories: "
            "importance, closest_work, argument_completeness, reproducibility, limitations. Every "
            "assessment and objection must cite IDs that occur in the ledger. closest_work must cite "
            "retrieved literature and explain the concrete difference. Numeric scores are insufficient. "
            "Do not claim novelty is proved. Open any objection that the present artifacts do not resolve. "
            f"Reviewer emphasis: {emphasis} Return only schema-conforming JSON."
        ),
        input=json.dumps({"manuscript_html": paper, "evidence_ledger": ledger},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )


def _request_identity(request: CompletionRequest, provider: str) -> dict[str, Any]:
    schema_digest = _bytes_digest(request.output_schema.read_bytes()) if request.output_schema else None
    return {
        "provider": provider,
        "model": request.model,
        "label": request.label,
        "instructions_sha256": _bytes_digest(request.prompt.instructions.encode()),
        "input_sha256": _bytes_digest(request.prompt.input.encode()),
        "output_schema_sha256": schema_digest,
        "allow_local_tools": request.allow_local_tools,
    }


def run_scientific_reviews(
    repo_root: Path,
    publication_dir: Path,
    model: str,
    transport: CompletionTransport | None = None,
) -> dict[str, Any]:
    """Run two isolated completions and persist all material needed for replay."""
    paper_path = publication_dir / "paper.html"
    ledger_path = publication_dir / "_drafts" / "evidence_ledger.json"
    try:
        paper_bytes = paper_path.read_bytes()
        paper = paper_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScientificReviewRunError(f"unreadable paper.html: {exc}") from exc
    ledger = _read_json(ledger_path, "evidence ledger")
    manuscript_digest = _bytes_digest(paper_bytes)
    ledger_digest = json_digest(ledger)
    completion_transport = transport or CodexCliAdapter()
    provider = type(completion_transport).__name__
    output_root = publication_dir / "scientific_reviews"
    output_root.mkdir(parents=True, exist_ok=True)
    review_ids: list[str] = []

    for index, (reviewer_id, emphasis) in enumerate(_REVIEWERS, start=1):
        review_id = f"review-{index}"
        prompt = _prompt(paper, ledger, emphasis)
        with tempfile.TemporaryDirectory(prefix=f"research-harness-{review_id}-") as raw:
            request = CompletionRequest(
                prompt=prompt,
                model=model,
                timeout_seconds=180,
                output_schema=_response_schema(repo_root),
                cwd=Path(raw),
                label=reviewer_id,
                allow_local_tools=False,
            )
            result = completion_transport.complete(request)
        try:
            response = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise ScientificReviewRunError(f"{review_id} returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise ScientificReviewRunError(f"{review_id} response must be an object")
        validate_schema(json.loads(_response_schema(repo_root).read_text()), response)
        identity = _request_identity(request, provider)
        record = {
            "version": 1,
            "review_id": review_id,
            "manuscript_sha256": manuscript_digest,
            "evidence_ledger_sha256": ledger_digest,
            "reviewer": {
                "reviewer_id": reviewer_id,
                "provider": provider,
                "model": model,
                "invocation_sha256": json_digest(identity),
            },
            "assessments": response["assessments"],
            "objections": response["objections"],
        }
        run_dir = output_root / review_id
        run_dir.mkdir(parents=True, exist_ok=False)
        prompt_payload = {"instructions": prompt.instructions, "input": prompt.input}
        completion = {"thread_id": result.thread_id, "usage": result.usage.as_dict()}
        files = {
            "prompt.json": json.dumps(prompt_payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            "raw_response.txt": result.text,
            "record.json": json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            "completion.json": json.dumps(completion, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            "request_identity.json": json.dumps(identity, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        }
        for name, content in files.items():
            (run_dir / name).write_text(content, encoding="utf-8")
        receipt = {
            "version": 1,
            "kind": "scientific_review_run",
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "files": {name: _bytes_digest((run_dir / name).read_bytes()) for name in sorted(files)},
        }
        (run_dir / "run_receipt.json").write_text(
            json.dumps(receipt, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        review_ids.append(review_id)
    return replay_scientific_reviews(publication_dir, expected_review_ids=review_ids)


def replay_scientific_reviews(
    publication_dir: Path,
    *,
    expected_review_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Reopen run artifacts and reconstruct trusted ``assess_readiness`` inputs."""
    paper_bytes = (publication_dir / "paper.html").read_bytes()
    ledger = _read_json(publication_dir / "_drafts" / "evidence_ledger.json", "evidence ledger")
    root = publication_dir / "scientific_reviews"
    review_ids = expected_review_ids or sorted(path.name for path in root.iterdir() if path.is_dir())
    if len(review_ids) != 2 or len(set(review_ids)) != 2:
        raise ScientificReviewRunError("exactly two scientific review runs are required")
    records: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    for review_id in review_ids:
        run_dir = root / review_id
        receipt = _read_json(run_dir / "run_receipt.json", "review run receipt")
        if receipt.get("kind") != "scientific_review_run" or receipt.get("review_id") != review_id:
            raise ScientificReviewRunError(f"invalid run receipt for {review_id}")
        files = receipt.get("files")
        if not isinstance(files, dict):
            raise ScientificReviewRunError(f"missing file hashes for {review_id}")
        for name, expected in files.items():
            path = run_dir / name
            if not path.is_file() or _bytes_digest(path.read_bytes()) != expected:
                raise ScientificReviewRunError(f"changed review artifact: {review_id}/{name}")
        prompt = _read_json(run_dir / "prompt.json", "review prompt")
        identity = _read_json(run_dir / "request_identity.json", "request identity")
        record = _read_json(run_dir / "record.json", "scientific review record")
        completion = _read_json(run_dir / "completion.json", "completion receipt")
        raw_response = (run_dir / "raw_response.txt").read_text(encoding="utf-8")
        try:
            parsed_response = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ScientificReviewRunError(f"changed raw response for {review_id}") from exc
        if record.get("assessments") != parsed_response.get("assessments") or record.get("objections") != parsed_response.get("objections"):
            raise ScientificReviewRunError(f"parsed record differs from raw response: {review_id}")
        if identity.get("instructions_sha256") != _bytes_digest(prompt["instructions"].encode()) or identity.get("input_sha256") != _bytes_digest(prompt["input"].encode()):
            raise ScientificReviewRunError(f"request identity differs from prompt: {review_id}")
        expected_input = json.dumps(
            {"manuscript_html": paper_bytes.decode("utf-8"), "evidence_ledger": ledger},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        if prompt.get("input") != expected_input:
            raise ScientificReviewRunError(f"review prompt is stale: {review_id}")
        reviewer = record.get("reviewer") or {}
        if (reviewer.get("reviewer_id") != receipt.get("reviewer_id")
                or reviewer.get("provider") != identity.get("provider")
                or reviewer.get("model") != identity.get("model")
                or reviewer.get("invocation_sha256") != json_digest(identity)):
            raise ScientificReviewRunError(f"review metadata differs from request: {review_id}")
        if not isinstance(completion.get("usage"), dict) or "thread_id" not in completion:
            raise ScientificReviewRunError(f"invalid completion receipt: {review_id}")
        run_receipt_sha256 = json_digest(receipt)
        records.append(record)
        artifacts.append({
            "kind": "verified_harness_review_run",
            "review_id": review_id,
            "reviewer_id": reviewer["reviewer_id"],
            "prompt_sha256": files["prompt.json"],
            "response_sha256": files["raw_response.txt"],
            "review_record_sha256": json_digest(record),
            "runner_receipt_sha256": run_receipt_sha256,
        })
    args = {
        "manuscript_digest": _bytes_digest(paper_bytes),
        "ledger": ledger,
        "review_records": records,
        "review_run_artifacts": artifacts,
        "require_verified_runs": True,
    }
    readiness = assess_readiness(**args)
    return {"readiness": readiness, "assess_readiness_args": args}
