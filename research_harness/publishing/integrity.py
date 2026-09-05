"""Bind a rendered preview to its writing inputs and verified research result."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class PublicationIntegrityError(ValueError):
    pass


def json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _entry(root: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise PublicationIntegrityError(f"publication file escapes its root: {path.name}")
    data = resolved.read_bytes()
    if not data:
        raise PublicationIntegrityError(f"empty publication file: {path.name}")
    return {"path": resolved.relative_to(root.resolve()).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def publication_inputs(production_dir: Path) -> list[dict[str, Any]]:
    paths = sorted(set((production_dir / "publication" / "_drafts").rglob("*.json"))
                   | set((production_dir / "rebuttal").rglob("*.json"))
                   | set((production_dir / "protocol_revisions").glob("*/approved.json")))
    return [_entry(production_dir, path) for path in paths]


def issue_publication_receipt(
    production_dir: Path, dispatch: dict[str, Any], strong_result_sha256: str,
    *, expected_inputs: list[dict[str, Any]] | None = None,
) -> str:
    """Commit an integrity receipt last; it certifies a preview, not readiness."""
    publication = production_dir / "publication"
    artifacts = dispatch.get("rendered_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PublicationIntegrityError("no rendered artifacts")
    outputs = []
    for item in artifacts:
        path = Path(item["artifact_path"])
        entry = _entry(publication, path)
        outputs.append({"output": item["output"], **entry})
    if "paper_html" not in {item["output"] for item in outputs}:
        raise PublicationIntegrityError("paper_html is required for the preview receipt")
    if len({item["path"] for item in outputs}) != len(outputs):
        raise PublicationIntegrityError("duplicate publication output")
    inputs = publication_inputs(production_dir)
    if not any(item["path"].startswith("publication/_drafts/") for item in inputs):
        raise PublicationIntegrityError("writing inputs missing")
    if expected_inputs is not None and inputs != expected_inputs:
        raise PublicationIntegrityError("writing inputs changed during rendering")
    resources = [_entry(publication, path) for path in sorted(
        (publication / "figures").rglob("*")) if path.is_file()]
    receipt = {"version": 1, "kind": "verified_preview_integrity",
               "strong_result_sha256": strong_result_sha256,
               "inputs": inputs, "outputs": outputs, "resources": resources}
    path = publication / "publication_receipt.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                                    indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return json_digest(receipt)


def verify_publication_receipt(
    production_dir: Path, dispatch: dict[str, Any], expected_digest: str,
    strong_result_sha256: str,
) -> bool:
    """Re-open every input and resource; a dispatch list alone has no authority."""
    publication = production_dir / "publication"
    try:
        receipt = json.loads((publication / "publication_receipt.json").read_text())
        if (json_digest(receipt) != expected_digest or receipt["version"] != 1
                or receipt["kind"] != "verified_preview_integrity"
                or receipt["strong_result_sha256"] != strong_result_sha256):
            return False
        for group, root in (("inputs", production_dir), ("outputs", publication),
                            ("resources", publication)):
            entries = receipt[group]
            if not isinstance(entries, list) or (group != "resources" and not entries):
                return False
            for entry in entries:
                actual = _entry(root, root / entry["path"])
                if any(actual[key] != entry[key] for key in actual):
                    return False
        if publication_inputs(production_dir) != receipt["inputs"]:
            return False
        actual_outputs = [{"output": item["output"], **_entry(
            publication, Path(item["artifact_path"]))}
            for item in dispatch["rendered_artifacts"]]
        return (actual_outputs == receipt["outputs"]
                and "paper_html" in {item["output"] for item in actual_outputs})
    except (OSError, ValueError, KeyError, TypeError):
        return False
