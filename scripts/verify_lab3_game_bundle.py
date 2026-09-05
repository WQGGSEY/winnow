#!/usr/bin/env python3
"""Compare game-only transitions to the original Lab3 engine, outside research input."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


TRACE = r'''
import hashlib, importlib.util, json, random, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("simulator", sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sim = module.Simulator()
trace_hash = hashlib.sha256()
steps = 0
score_changes = 0
food_changes = 0
for board in sorted((module.Path(sys.argv[2]).parent / "layouts").glob("*.lay")):
    for seed in range(4):
        rng = random.Random(seed)
        state = sim.reset(layout=board.stem, seed=seed)
        while not state["terminal"]:
            before = state
            state = sim.step(agent=state["current_agent"], action=rng.choice(state["legal_actions"]))
            trace_hash.update(json.dumps(state, sort_keys=True).encode())
            steps += 1
            score_changes += bool(state["reward"]["red"])
            food_changes += before["food"] != state["food"]
        try:
            sim.step(agent=0, action="Stop")
            raise AssertionError("terminal step accepted")
        except ValueError:
            pass
state = sim.reset(max_moves=1, starting_agent=0)
for kwargs in ({"agent": 1, "action": "Stop"}, {"agent": 0, "action": "invalid"}):
    try:
        sim.step(**kwargs)
        raise AssertionError("invalid action accepted")
    except ValueError:
        assert sim.observe() == state
assert sim.step(agent=0, action="Stop")["terminal"]
print(json.dumps({"trace_sha256": trace_hash.hexdigest(), "moves": steps,
                  "score_changes": score_changes, "food_changes": food_changes}))
'''


def verify(bundle: Path, original: Path, original_python: str, bundle_python: str):
    manifest = json.loads((bundle / "input_manifest.json").read_text())
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    assert actual == set(manifest["files"]) | {"input_manifest.json"}, "unexpected input files"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest, name
    for name, digest in manifest["source_sha256"].items():
        assert hashlib.sha256((original / "minicontest" / name).read_bytes()).hexdigest() == digest, name
    outputs = []
    for python, engine in ((original_python, original / "minicontest"), (bundle_python, bundle)):
        result = subprocess.run(
            [python, "-B", "-c", TRACE, str(engine), str(bundle / "simulator.py")],
            text=True, capture_output=True, check=True,
        )
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1], "extracted engine changed the transition trace"
    assert outputs[1]["score_changes"] > 0 and outputs[1]["food_changes"] > 0
    print(json.dumps({"status": "passed", **outputs[1]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("original", type=Path)
    parser.add_argument("--original-python", default="python")
    parser.add_argument("--bundle-python", default="python")
    args = parser.parse_args()
    verify(args.bundle.resolve(), args.original.resolve(), args.original_python, args.bundle_python)
