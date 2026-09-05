"""Server-side figure renderer for the Sakana-v2-style paper writer.

Claude Code (acting as paper writer) calls register_paper_figure with a
figure_type + data_spec. The server pulls the referenced data from the
worker_report / ac_decision / etc. and renders a deterministic PNG. The
LLM does NOT draw figures — the LLM picks the figure type and the data
keys, and the server does the rendering. This keeps figures auditable
and reproducible.

Supported figure_type values:
- loco_heatmap         — region x asset AUC heatmap from worker_report.metrics.loco_auc_per_cell
- baseline_bars        — bar chart of main metric vs each baseline value
- ablation_drops       — bar chart of metric drop when each feature block is removed
- lift_ci_forest       — forest plot of lift estimates with CIs across conditions
- score_radar          — internal report radar chart of critic_score_summary axes
- claim_tree_status    — internal report graph showing root + promoted/pruned children
- metric_table         — rendered table image of a worker_report dict
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import matplotlib
matplotlib.use("Agg")  # non-interactive backend, safe for headless renders
import matplotlib.pyplot as plt


class FigureRenderError(ValueError):
    """Raised when a figure cannot be drawn — missing data_spec key,
    non-numeric values, unsupported figure_type, etc."""


MANUSCRIPT_BLOCKED_FIGURE_TYPES = frozenset({"score_radar", "claim_tree_status"})


@dataclass(frozen=True, slots=True)
class SourceProjection:
    """Evidence slice used to render a figure, plus a stable digest for receipts."""

    source_paths: tuple[str, ...]
    value: Any
    digest: str


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dotpath(state: Mapping[str, Any], path: str) -> Any:
    if not isinstance(path, str) or not path.strip():
        raise FigureRenderError("data_spec path must be a non-empty string")
    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            raise FigureRenderError(f"data_spec path not found: {path!r}")
    return cur


def _require_worker_report_path(path: str, *, field: str) -> str:
    if not isinstance(path, str) or not path.startswith("worker_report."):
        raise FigureRenderError(f"{field} must be a worker_report dot-path")
    return path


def figure_source_projection(
    figure_type: str,
    data_spec: dict[str, Any],
    worker_report: dict[str, Any],
    ac_decision: dict[str, Any],
) -> SourceProjection:
    """Return the exact evidence projection a rendered figure will consume."""

    state = {"worker_report": worker_report, "ac_decision": ac_decision}
    spec = data_spec or {}
    if figure_type == "loco_heatmap":
        path = _require_worker_report_path(
            spec.get("cells_key") or "worker_report.metrics.loco_auc_per_cell",
            field="loco_heatmap.cells_key",
        )
        value = _dotpath(state, path)
        return SourceProjection((path,), value, _digest({"paths": [path], "value": value}))
    if figure_type == "baseline_bars":
        main_path = _require_worker_report_path(
            spec.get("main_metric_key") or "worker_report.metrics.auc_overall",
            field="baseline_bars.main_metric_key",
        )
        baseline_path = _require_worker_report_path(
            spec.get("baselines_key") or "worker_report.baselines",
            field="baseline_bars.baselines_key",
        )
        value = {
            "main": _dotpath(state, main_path),
            "baselines": _dotpath(state, baseline_path),
        }
        return SourceProjection((main_path, baseline_path), value, _digest({"paths": [main_path, baseline_path], "value": value}))
    if figure_type == "ablation_drops":
        if "drops" in spec:
            raise FigureRenderError("ablation_drops.drops inline data is not allowed; use drops_key")
        path = _require_worker_report_path(
            spec.get("drops_key") or "worker_report.metrics.ablation_drops",
            field="ablation_drops.drops_key",
        )
        value = _dotpath(state, path)
        return SourceProjection((path,), value, _digest({"paths": [path], "value": value}))
    if figure_type == "lift_ci_forest":
        if "entries" in spec:
            raise FigureRenderError("lift_ci_forest.entries inline data is not allowed; use entries_key or point/ci paths")
        entries_key = spec.get("entries_key")
        if entries_key:
            path = _require_worker_report_path(entries_key, field="lift_ci_forest.entries_key")
            value = _dotpath(state, path)
            return SourceProjection((path,), value, _digest({"paths": [path], "value": value}))
        paths = (
            _require_worker_report_path(
                spec.get("point_key") or "worker_report.metrics.lift_over_naive_auc",
                field="lift_ci_forest.point_key",
            ),
            _require_worker_report_path(
                spec.get("ci_low_key") or "worker_report.metrics.lift_over_naive_ci95_low",
                field="lift_ci_forest.ci_low_key",
            ),
            _require_worker_report_path(
                spec.get("ci_high_key") or "worker_report.metrics.lift_over_naive_ci95_high",
                field="lift_ci_forest.ci_high_key",
            ),
        )
        value = {"label": spec.get("label") or "overall"}
        value.update({path: _dotpath(state, path) for path in paths})
        return SourceProjection(paths, value, _digest({"paths": list(paths), "value": value}))
    if figure_type == "metric_table":
        if "rows" in spec:
            raise FigureRenderError("metric_table.rows inline data is not allowed; use dict_key")
        path = _require_worker_report_path(
            spec.get("dict_key") or "worker_report.metrics",
            field="metric_table.dict_key",
        )
        value = _dotpath(state, path)
        return SourceProjection((path,), value, _digest({"paths": [path], "value": value}))
    if figure_type == "score_radar":
        path = spec.get("score_summary_key") or "ac_decision.score_summary"
        value = _dotpath(state, path)
        return SourceProjection((path,), value, _digest({"paths": [path], "value": value}))
    if figure_type == "claim_tree_status":
        if "nodes" in spec:
            raise FigureRenderError("claim_tree_status.nodes inline data is not allowed; use nodes_key")
        path = spec.get("nodes_key")
        if not path:
            raise FigureRenderError("claim_tree_status.nodes_key is required")
        value = _dotpath(state, path)
        return SourceProjection((path,), value, _digest({"paths": [path], "value": value}))
    raise FigureRenderError(f"unsupported figure_type: {figure_type!r}")


def render_figure(
    figure_id: str,
    figure_type: str,
    caption: str,
    data_spec: dict[str, Any],
    worker_report: dict[str, Any],
    ac_decision: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Render one figure. Returns the absolute path to the written PNG."""
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"{figure_id}.png"

    state = {"worker_report": worker_report, "ac_decision": ac_decision}

    figure_source_projection(
        figure_type,
        data_spec,
        worker_report,
        ac_decision,
    )

    if figure_type == "loco_heatmap":
        _draw_loco_heatmap(artifact, data_spec, state)
    elif figure_type == "baseline_bars":
        _draw_baseline_bars(artifact, data_spec, state)
    elif figure_type == "ablation_drops":
        _draw_ablation_drops(artifact, data_spec, state)
    elif figure_type == "lift_ci_forest":
        _draw_lift_ci_forest(artifact, data_spec, state)
    elif figure_type == "score_radar":
        _draw_score_radar(artifact, data_spec, state)
    elif figure_type == "claim_tree_status":
        _draw_claim_tree_status(artifact, data_spec, state)
    elif figure_type == "metric_table":
        _draw_metric_table(artifact, data_spec, state)
    else:
        raise FigureRenderError(f"unsupported figure_type: {figure_type!r}")

    return artifact


# --- per-figure renderers ------------------------------------------------- #


def _draw_loco_heatmap(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec: { 'cells_key': 'worker_report.metrics.loco_auc_per_cell' }
    where cells_key resolves to either:
      - dict[str, float]  e.g. {"US__equities": 0.66, ...} — auto split into region/asset
      - dict[str, dict[str, float]] — pre-shaped { region: { asset: auc } }
    """
    cells_key = spec.get("cells_key") or "worker_report.metrics.loco_auc_per_cell"
    cells = _dotpath(state, cells_key)

    if isinstance(cells, dict) and cells and isinstance(next(iter(cells.values())), (int, float)):
        regions: list[str] = []
        assets: list[str] = []
        matrix: dict[tuple[str, str], float] = {}
        for k, v in cells.items():
            if "__" in k:
                r, a = k.split("__", 1)
            else:
                r, a = k, "all"
            if r not in regions:
                regions.append(r)
            if a not in assets:
                assets.append(a)
            matrix[(r, a)] = float(v)
        Z = [[matrix.get((r, a), float("nan")) for a in assets] for r in regions]
    elif isinstance(cells, dict):
        regions = list(cells.keys())
        all_assets: list[str] = []
        for v in cells.values():
            for a in v.keys():
                if a not in all_assets:
                    all_assets.append(a)
        assets = all_assets
        Z = [[float(cells[r].get(a, float("nan"))) for a in assets] for r in regions]
    else:
        raise FigureRenderError("loco_heatmap cells must be dict[str,float] or dict[str,dict]")

    fig, ax = plt.subplots(figsize=(max(4, 0.8 * len(assets) + 2), max(3, 0.5 * len(regions) + 2)))
    im = ax.imshow(Z, cmap="RdYlGn", vmin=0.5, vmax=1.0, aspect="auto")
    ax.set_xticks(range(len(assets)))
    ax.set_xticklabels(assets, rotation=30, ha="right")
    ax.set_yticks(range(len(regions)))
    ax.set_yticklabels(regions)
    for i, row in enumerate(Z):
        for j, v in enumerate(row):
            if not math.isnan(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", color="black", fontsize=9)
    ax.set_title("LOCO AUC per region x asset cell")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("AUC")
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _draw_baseline_bars(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec: { 'main_metric_key': 'worker_report.metrics.auc_overall',
                    'baselines_key': 'worker_report.baselines' }
    """
    main_key = spec.get("main_metric_key") or "worker_report.metrics.auc_overall"
    base_key = spec.get("baselines_key") or "worker_report.baselines"
    main_value = float(_dotpath(state, main_key))
    baselines = _dotpath(state, base_key)
    if not isinstance(baselines, dict) or not baselines:
        raise FigureRenderError(f"baseline_bars: {base_key!r} must be a non-empty dict")
    labels = ["ours", *baselines.keys()]
    values = [main_value, *(float(v) for v in baselines.values())]
    colors = ["#2845a8", *["#7a87b8"] * len(baselines)]
    fig, ax = plt.subplots(figsize=(max(4, 0.9 * len(labels) + 1), 4))
    ax.bar(labels, values, color=colors, edgecolor="#1c2a55")
    for i, v in enumerate(values):
        ax.text(i, v + 0.01, f"{v:.3f}", ha="center", fontsize=9)
    ax.set_ylabel("score")
    ax.set_title(spec.get("title") or "Ours vs baselines")
    ax.set_ylim(0, max(1.05, max(values) * 1.1))
    plt.xticks(rotation=20, ha="right")
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _draw_ablation_drops(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec: { 'drops_key': 'worker_report.metrics.ablation_drops' }
    drops_key resolves to dict[str, float] of feature-block -> drop magnitude.
    """
    drops_key = spec.get("drops_key") or "worker_report.metrics.ablation_drops"
    d = _dotpath(state, drops_key)
    if not isinstance(d, dict) or not d:
        raise FigureRenderError(f"ablation_drops: {drops_key!r} must be a non-empty dict")
    items = [(k, float(v)) for k, v in d.items()]
    items.sort(key=lambda kv: kv[1], reverse=True)
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    threshold = float(spec.get("leakage_threshold", 0.10))
    fig, ax = plt.subplots(figsize=(max(4, 0.9 * len(labels) + 1), 4))
    bars = ax.bar(labels, values, color=["#c25450" if v >= threshold else "#5fa05f" for v in values])
    ax.axhline(threshold, color="#888", linestyle="--", linewidth=1)
    ax.text(len(labels) - 0.5, threshold + 0.005, f"leakage line {threshold:.2f}", color="#444", fontsize=9, ha="right")
    for i, v in enumerate(values):
        ax.text(i, v + 0.005, f"{v:.3f}", ha="center", fontsize=9)
    ax.set_ylabel("AUC drop when block removed")
    ax.set_title(spec.get("title") or "Feature-block ablation drops")
    plt.xticks(rotation=20, ha="right")
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _draw_lift_ci_forest(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec entries must come from worker_report paths."""
    entries_key = spec.get("entries_key")
    if entries_key:
        entries = _dotpath(state, entries_key)
        if not isinstance(entries, list) or not entries:
            raise FigureRenderError(f"lift_ci_forest: {entries_key!r} must resolve to a non-empty list")
    else:
        point = float(_dotpath(state, spec.get("point_key") or "worker_report.metrics.lift_over_naive_auc"))
        lo = float(_dotpath(state, spec.get("ci_low_key") or "worker_report.metrics.lift_over_naive_ci95_low"))
        hi = float(_dotpath(state, spec.get("ci_high_key") or "worker_report.metrics.lift_over_naive_ci95_high"))
        entries = [{"label": "overall", "point": point, "ci_low": lo, "ci_high": hi}]

    labels = [e["label"] for e in entries]
    points = [float(e["point"]) for e in entries]
    lows = [float(e["ci_low"]) for e in entries]
    highs = [float(e["ci_high"]) for e in entries]
    ys = list(range(len(entries)))
    fig, ax = plt.subplots(figsize=(6, max(2.5, 0.5 * len(entries) + 1.5)))
    for y, p, lo, hi in zip(ys, points, lows, highs):
        ax.plot([lo, hi], [y, y], color="#2845a8", linewidth=2)
        ax.scatter([p], [y], color="#2845a8", zorder=3, s=40)
        ax.text(hi + 0.005, y, f"{p:+.3f}", va="center", fontsize=9)
    ax.axvline(0, color="#999", linestyle="--", linewidth=1)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("lift over naive (95% CI)")
    ax.set_title(spec.get("title") or "Lift estimates with 95% CI")
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _draw_score_radar(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec: { 'score_summary_key': 'ac_decision.score_summary' }"""
    key = spec.get("score_summary_key") or "ac_decision.score_summary"
    scores = _dotpath(state, key)
    if not isinstance(scores, dict) or not scores:
        raise FigureRenderError(f"score_radar: {key!r} must be a non-empty dict")
    axes_labels = list(scores.keys())
    values = [float(scores[a]) for a in axes_labels]
    N = len(axes_labels)
    angles = [n / N * 2 * math.pi for n in range(N)]
    angles += [angles[0]]
    values_closed = values + [values[0]]
    fig, ax = plt.subplots(figsize=(5, 5), subplot_kw={"projection": "polar"})
    ax.plot(angles, values_closed, color="#2845a8", linewidth=2)
    ax.fill(angles, values_closed, color="#2845a8", alpha=0.18)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(axes_labels, fontsize=9)
    ax.set_yticks([2, 4, 6, 8, 10])
    ax.set_ylim(0, 10)
    ax.set_title(spec.get("title") or "AC score summary (1-10)", y=1.08)
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _draw_claim_tree_status(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec.nodes_key resolves to [{id, status, node_type, parent}, ...]."""
    nodes = _dotpath(state, spec.get("nodes_key"))
    if not nodes:
        raise FigureRenderError("claim_tree_status: nodes_key must resolve to nodes")
    parents = {n["id"]: n.get("parent") for n in nodes}
    children: dict[str | None, list[str]] = {}
    for n in nodes:
        children.setdefault(n.get("parent"), []).append(n["id"])
    # Depth via BFS from roots (parent=None).
    depth: dict[str, int] = {}
    order: list[str] = []
    stack = [(rid, 0) for rid in children.get(None, [])]
    while stack:
        nid, d = stack.pop(0)
        depth[nid] = d
        order.append(nid)
        for c in children.get(nid, []):
            stack.append((c, d + 1))
    by_depth: dict[int, list[str]] = {}
    for nid, d in depth.items():
        by_depth.setdefault(d, []).append(nid)

    color_for = {"promoted": "#5fa05f", "pruned": "#c25450", "running": "#e0a14d", "ready": "#9aaad8"}
    pos: dict[str, tuple[float, float]] = {}
    for d, ids in by_depth.items():
        for i, nid in enumerate(ids):
            pos[nid] = (i - (len(ids) - 1) / 2.0, -d)
    fig, ax = plt.subplots(figsize=(7, max(3, len(by_depth) * 1.8)))
    for n in nodes:
        for c in children.get(n["id"], []):
            x0, y0 = pos[n["id"]]
            x1, y1 = pos[c]
            ax.plot([x0, x1], [y0, y1], color="#888", linewidth=1)
    for n in nodes:
        x, y = pos[n["id"]]
        ax.scatter([x], [y], s=550, color=color_for.get(n.get("status", "ready"), "#9aaad8"), edgecolor="#222", zorder=3)
        label = n.get("short_label") or n["id"]
        if len(label) > 18:
            label = label[:18] + "…"
        ax.text(x, y - 0.32, label, ha="center", fontsize=8)
    ax.set_axis_off()
    ax.set_title(spec.get("title") or "Claim tree — promoted/pruned status")
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _draw_metric_table(path: Path, spec: dict[str, Any], state: dict[str, Any]) -> None:
    """data_spec.dict_key resolves to a worker_report dict."""
    dict_key = spec.get("dict_key") or "worker_report.metrics"
    d = _dotpath(state, dict_key)
    if not isinstance(d, dict):
        raise FigureRenderError(f"metric_table: {dict_key!r} must resolve to a dict")
    rows = [(k, v) for k, v in d.items()]

    fig_height = max(1.2, 0.35 * len(rows) + 0.6)
    fig, ax = plt.subplots(figsize=(6, fig_height))
    ax.axis("off")
    table_data = [[k, _fmt_value(v)] for k, v in rows]
    table = ax.table(cellText=table_data, colLabels=["metric", "value"], cellLoc="left", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.3)
    ax.set_title(spec.get("title") or "Headline metrics", fontsize=11)
    plt.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    if isinstance(v, int):
        return str(v)
    return str(v)
