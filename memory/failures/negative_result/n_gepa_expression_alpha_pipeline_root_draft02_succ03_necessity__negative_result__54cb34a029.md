---
node_id: n_gepa_expression_alpha_pipeline_root_draft02_succ03_necessity
thread_id: thread_43cf2ea8
category: negative_result
transition: pruned
final_verdict: contradicted
tags: ['automated_expression_alpha_generation', 'automation', 'dataset_selection', 'expression_alpha_generation', 'llm_gradient_flow_GEPA', 'metadata_guided_search', 'necessity', 'num_drafts', 'professor_follow_up', 'simulator_fidelity', 'taste', 'validity', 'wq_brain_submission_constraints']
source: mcp_server
---

# Failure: n_gepa_expression_alpha_pipeline_root_draft02_succ03_necessity

**Claim under test:**
> Within the v2 apparatus, the Pareto-frontier reflection step (keep top-K, regenerate bottom-K across 2 iterations) is necessary above and beyond just structural leaf-seeding: an ablated pipeline that uses the same leaf-seeded init pop=24 but SKIPS the Pareto reflection (single-shot evaluation only) achieves gepa_top5_mean_ic < 0.5 × the v2 baseline (i.e. recovery_ratio drops to <= 0.34) and reflection_gain_delta drops below 0.030 with paired t-test p > 0.10. The necessity axis: leaf-seeding alone is not sufficient — Pareto-frontier reflection contributes a measurable IC delta independent of the structural-proposal step.

**Professor verdict (pruned):** contradicted

**Headline metrics:** field_selection_binomial_p=0.0, gepa_field_hit_rate_ge2_in_top3=0.9, gepa_top5_mean_ic=0.0817, miprov2_top5_mean_ic=0.01, necessity_axis_note=ablation skips 2 Pareto-frontier reflection iterations; keeps leaf-seeded structural proposer + metadata-bias, necessity_predictions_hit=[], necessity_predictions_total=2, no_pareto_field_hit_rate_ge2_in_top3=0.65, no_pareto_field_selection_binomial_p=0.0, no_pareto_recovery_ratio=0.4061, no_pareto_top5_mean_ic=0.0488, no_pareto_vs_mipro_delta_mean=0.0388, no_pareto_vs_mipro_delta_p_one_sided=0.0, pareto_lift_full_minus_no_pareto=0.033, planted_ground_truth_ic=0.1201, random_top5_mean_ic=0.0035, reflection_gain_delta_ci95_hi=0.092, reflection_gain_delta_ci95_lo=0.0546, reflection_gain_delta_mean=0.0718, reflection_gain_delta_p_one_sided=0.0, seeded_runs=20.0, total_wall_clock_s=84.4362

**Lesson (from Professor's response):**

Strong-form Pareto-necessity claim REFUTED, parallel pattern to the metadata-bias ablation. Predicted: no_pareto recovery_ratio drops to <= 0.34 and reflection_gain_delta drops below 0.030 with p > 0.10. Observed: no_pareto_recovery_ratio = 0.41 (above the 0.34 threshold), no_pareto_vs_mipro_delta = 0.039 with paired t-test p = 1.06e-06 (clearly significant, NOT collapsed). Zero of two strong predictions hit. The Pareto reflection contributes a measurable +0.033 IC lift (pareto_lift_full_minus_no_pareto: from 0.049 → 0.082, ~40% of the full pipeline's gain over MIPROv2), but the leaf-seeded structural proposer alone (no Pareto) still cleanly beats MIPROv2 by 0.039 IC absolute with p=1e-06. Convergent mechanism story across both necessity ablations: STRUCTURAL LEAF-SEEDING (driven by the metadata-tagged top-bias fields) is the load-bearing piece; Pareto reflection and metadata-bias are each contributory amplifiers worth ~0.03 IC absolute, neither is strictly necessary. The full pipeline still clears all 5 success criteria. Pruning with the substantive lesson recorded; the architectural story is now: dual-arm pipeline's lift is dominated by the metadata-aware structural-proposal step (GEPA paper §3.2 selector module's typed-candidate proposal), with Pareto refinement and field-bias weighting as smaller-magnitude amplifiers.
