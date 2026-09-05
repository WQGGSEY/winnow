"""Phase E tests: figure renderer covers all 7 supported types."""

from __future__ import annotations

import pytest

from research_harness.publishing.figures import (
    FigureRenderError,
    figure_source_projection,
    render_figure,
)


def _wr():
    return {
        "metrics": {
            "auc_overall": 0.968,
            "lift_over_naive_auc": 0.435,
            "lift_over_naive_ci95_low": 0.343,
            "lift_over_naive_ci95_high": 0.523,
            "loco_auc_per_cell": {
                "US__equities": 0.664, "EU__equities": 0.91, "ASIA__equities": 1.0,
            },
            "ablation_drops": {"footprint": 0.09, "is": 0.005},
            "lift_entries": [
                {"label": "small", "point": 0.24, "ci_low": 0.12, "ci_high": 0.31},
                {"label": "large", "point": 0.43, "ci_low": 0.34, "ci_high": 0.52},
            ],
        },
        "baselines": {"current_best_known": 0.70, "naive": 0.533, "random_or_null": 0.510},
    }


def _ac():
    return {
        "score_summary": {"validity": 8, "necessity": 7, "novelty": 7,
                           "clarity": 7, "reproducibility": 7, "taste_alignment": 8},
        "tree_nodes": [
            {"id": "root", "parent": None, "status": "promoted"},
            {"id": "succ", "parent": "root", "status": "pruned"},
        ],
    }


@pytest.mark.parametrize("ft,ds", [
    ("loco_heatmap", {}),
    ("baseline_bars", {}),
    ("ablation_drops", {}),
    ("lift_ci_forest", {}),
    ("score_radar", {}),
    ("metric_table", {}),
    ("claim_tree_status", {"nodes_key": "ac_decision.tree_nodes"}),
])
def test_render_figure_types(tmp_path, ft, ds):
    p = render_figure(f"f_test_{ft}", ft, "cap", ds, _wr(), _ac(), tmp_path)
    assert p.exists()
    assert p.stat().st_size > 500


def test_unsupported_figure_type(tmp_path):
    with pytest.raises(FigureRenderError, match="unsupported"):
        render_figure("f_x", "bogus_type", "cap", {}, _wr(), _ac(), tmp_path)


def test_missing_data_spec_path(tmp_path):
    with pytest.raises(FigureRenderError):
        render_figure("f_x", "score_radar", "cap", {"score_summary_key": "no.such.path"}, _wr(), {}, tmp_path)


@pytest.mark.parametrize("ft,ds,match", [
    ("ablation_drops", {"drops": [{"name": "x", "drop": 0.1}]}, "inline data"),
    ("lift_ci_forest", {"entries": [{"label": "x", "point": 0.1, "ci_low": 0.0, "ci_high": 0.2}]}, "inline data"),
    ("metric_table", {"rows": [{"label": "x", "value": 1}]}, "inline data"),
])
def test_quantitative_figures_reject_inline_data(tmp_path, ft, ds, match):
    with pytest.raises(FigureRenderError, match=match):
        render_figure(f"f_{ft}", ft, "cap", ds, _wr(), _ac(), tmp_path)


def test_loco_heatmap_rejects_synthesized_min_max_fallback(tmp_path):
    wr = {"metrics": {"loco_auc_min": 0.61, "loco_auc_max": 0.89}, "baselines": {}}
    with pytest.raises(FigureRenderError, match="loco_auc_per_cell"):
        render_figure("f_loco", "loco_heatmap", "cap", {}, wr, _ac(), tmp_path)


def test_source_projection_digest_tracks_worker_report_values():
    before = figure_source_projection("baseline_bars", {}, _wr(), _ac())
    changed = _wr()
    changed["metrics"]["auc_overall"] = 0.5
    after = figure_source_projection("baseline_bars", {}, changed, _ac())
    assert before.source_paths == ("worker_report.metrics.auc_overall", "worker_report.baselines")
    assert before.digest != after.digest
