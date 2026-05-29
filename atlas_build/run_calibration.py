"""ADR 0010 — offline keystone read: does behavioral co-deployment out-separate
the strong topical (SPECTER) baseline on the frozen held-out, by the permutation
delta? Run OFFLINE (atlas-builder extra). Heavily caveated: the solver is the
same model that has seen the held-out -> FAILURE-INFORMATIVE only."""

import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import json
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent
sys.path.insert(0, str(BUILD.parent))  # repo root, for research_harness import

from research_harness.atlas import distance as D  # noqa: E402

nodes = json.loads((BUILD / "method_nodes.json").read_text())["nodes"]
sols_bundle = json.loads((BUILD / "battery" / "solutions.json").read_text())
sols = sols_bundle["solutions"]
heldout = json.loads((BUILD / "heldout_preregistered.json").read_text())
node_ids = [n["id"] for n in nodes]

# 1. BEHAVIORAL detection: scan each worked solution for node signatures.
deployments = [D.detect_methods(text, nodes) for text in sols.values()]
print("=== deployments per problem (behavioral) ===")
for pid, dep in zip(sols, deployments):
    print(f"  {pid}: {sorted(dep)}")

# 2. STRONG topical baseline (SPECTER), kept separate from the primary.
import numpy as np  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

topical_model_id, st, last_err = None, None, None
for cand in ("sentence-transformers/allenai-specter", "allenai/specter2_base"):
    try:
        st = SentenceTransformer(cand)
        topical_model_id = cand
        break
    except Exception as exc:  # noqa: BLE001
        last_err = exc
if st is None:
    raise SystemExit(f"could not load a SPECTER topical model: {last_err}")
print(f"\n=== topical baseline: {topical_model_id} ===")
descs = [f"{n['label']}. {n['description']}" for n in nodes]
emb = st.encode(descs, normalize_embeddings=True)
topical = {}
for i in range(len(node_ids)):
    for j in range(i + 1, len(node_ids)):
        cos = float(np.dot(emb[i], emb[j]))
        topical[tuple(sorted((node_ids[i], node_ids[j])))] = max(0.0, min(1.0, 1.0 - (cos + 1.0) / 2.0))

# 3. Calibration gate (frozen held-out + permutation delta).
out = D.run_calibration(deployments=deployments, nodes=nodes, topical_distances=topical, heldout=heldout)
out["measuring_model_id"] = sols_bundle.get("measuring_model_id")
out["topical_model_id"] = topical_model_id
out["caveat"] = (
    "FAILURE-INFORMATIVE first read: solver == measuring model has seen the held-out; "
    "a fail is trustworthy, a pass is inconclusive and needs an unexposed model."
)

print("\n=== KEYSTONE READ ===")
print(json.dumps({k: v for k, v in out.items() if k != "co_deployment_distances"}, indent=2, ensure_ascii=False))
(BUILD / "calibration_result.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"\nwrote {BUILD / 'calibration_result.json'}")
