#!/usr/bin/env python3
"""Export only pinned Lab3 simulation sources into a fresh directory."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess


REVISION = "1468141dd5706ddbb5d4dcb4f5491337cbbf7768"
LAYOUTS = (
    "alley", "blox", "crowded", "default", "distant", "fast", "jumbo",
    "medium", "office", "strategic", "test", "tiny",
)


def build(source_repo: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("Output must be a new directory; stale inputs are not allowed")
    files = {}
    source_hashes = {}
    names = ["capture.py", "game.py", "layout.py", "util.py", "mazeGenerator.py"]
    names += [f"layouts/{name}Capture.lay" for name in LAYOUTS]
    for name in names:
        raw = subprocess.run(
            ["git", "-C", str(source_repo), "show", f"{REVISION}:minicontest/{name}"],
            check=True, capture_output=True,
        ).stdout
        source_hashes[name] = hashlib.sha256(raw).hexdigest()
        if name == "capture.py":
            source = raw.decode()
            lines = source.splitlines(keepends=True)
            # Retain original notices and simulation rules. Exclude the CLI,
            # dynamic policy loader, keyboard policies, and pandas score export.
            nodes = ast.parse(source).body
            end = next(node.lineno - 1 for node in nodes
                       if isinstance(node, ast.FunctionDef) and node.name == "default")
            excluded = set()
            for node in nodes:
                if isinstance(node, ast.Import) and any(
                    alias.name in {"imp", "keyboardAgents", "pandas"}
                    for alias in node.names
                ):
                    excluded.update(range(node.lineno - 1, node.end_lineno))
            source = "".join(line for i, line in enumerate(lines[:end]) if i not in excluded)
            raw = (source + "\nimport sys, util, random\n").encode()
        files[name] = raw
    scripts = Path(__file__).resolve().parent
    files["simulator.py"] = (scripts / "lab3_simulator.py").read_bytes()
    files["README.md"] = (scripts.parent / "docs/research/lab3-game-interface.md").read_bytes()
    manifest = {
        "kind": "game_only_input", "upstream_revision": REVISION,
        "upstream": "https://github.com/WQGGSEY/Lab3",
        "source_sha256": source_hashes,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "provided_policies": [], "provided_training_code": [],
    }
    output.mkdir(parents=True)
    for name, raw in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (output / "input_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_repo", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source_repo, args.output)
