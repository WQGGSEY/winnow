"""Phase E tests: figure renderer covers all 7 supported types."""

from __future__ import annotations

import pytest

from research_harness.publishing.figures import FigureRenderError, render_figure


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
        },
        "baselines": {"current_best_known": 0.70, "naive": 0.533, "random_or_null": 0.510},
    }


def _ac():
    return {"score_summary": {"validity": 8, "necessity": 7, "novelty": 7,
                               "clarity": 7, "reproducibility": 7, "taste_alignment": 8}}


@pytest.mark.parametrize("ft,ds", [
    ("loco_heatmap", {}),
    ("baseline_bars", {}),
    ("ablation_drops", {"drops": [{"name": "footprint", "drop": 0.09}, {"name": "is", "drop": 0.005}]}),
    ("lift_ci_forest", {}),
    ("score_radar", {}),
    ("metric_table", {}),
    ("claim_tree_status", {"nodes": [
        {"id": "root", "parent": None, "status": "promoted"},
        {"id": "succ", "parent": "root", "status": "pruned"},
    ]}),
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
