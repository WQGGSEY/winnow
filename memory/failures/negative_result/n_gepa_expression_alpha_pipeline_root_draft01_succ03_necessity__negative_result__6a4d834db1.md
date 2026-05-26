---
node_id: n_gepa_expression_alpha_pipeline_root_draft01_succ03_necessity
thread_id: thread_43cf2ea8
category: negative_result
transition: pruned
final_verdict: contradicted
tags: ['automated_expression_alpha_generation', 'automation', 'dataset_selection', 'expression_alpha_generation', 'llm_gradient_flow_GEPA', 'metadata_guided_search', 'necessity', 'num_drafts', 'operational', 'professor_follow_up', 'simulator_fidelity', 'validity', 'wq_brain_submission_constraints']
source: mcp_server
---

# Failure: n_gepa_expression_alpha_pipeline_root_draft01_succ03_necessity

**Claim under test:**
> Within the same calibrated v2 apparatus, the metadata-bias mechanism (the _metadata_bias function biasing population sampling toward the 40-70th submitted-alpha-pool-count percentile where planted fields are placed) is necessary, not merely sufficient: an ablated GEPA-analog that drops metadata bias (uniform field draws but keeps the leaf-seeded structural proposer and Pareto reflection) achieves gepa_field_hit_rate_ge2_in_top3 < 0.30 and field_selection_binomial_p > 0.10, while keeping reflection-gain delta < 0.005. Necessity axis: removing the metadata-conditioning step degrades the selection arm AND the reflection-gain signature to indistinguishable-from-MIPROv2 levels, demonstrating metadata-conditioning is the load-bearing piece, not random Pareto evolution.

**Professor verdict (pruned):** contradicted

**Headline metrics:** ablated_field_hit_rate_ge2_in_top3=0.5, ablated_field_selection_binomial_p=0.0, ablated_top5_mean_ic=0.0391, ablated_vs_mipro_delta_ci95_hi=0.0397, ablated_vs_mipro_delta_ci95_lo=0.0197, ablated_vs_mipro_delta_mean=0.0291, ablated_vs_mipro_delta_p_one_sided=0.0, field_selection_binomial_p=0.0, gepa_field_hit_rate_ge2_in_top3=0.9, gepa_top5_mean_ic=0.0817, miprov2_top5_mean_ic=0.01, necessity_axis_note=ablation drops metadata-bias from both leaf-seeding and random exploration; keeps leaf-seeded structural proposer (uniform top-8 fields) and Pareto reflection, necessity_predictions_hit=[], necessity_predictions_total=3, planted_ground_truth_ic=0.1201, random_top5_mean_ic=0.0035, reflection_gain_delta_ci95_hi=0.092, reflection_gain_delta_ci95_lo=0.0546, reflection_gain_delta_mean=0.0718, reflection_gain_delta_p_one_sided=0.0, seeded_runs=20.0, total_wall_clock_s=116.7835

**Lesson (from Professor's response):**

Strong-form necessity claim REFUTED in a useful direction. Hypothesis: removing metadata-bias from GEPA-analog should degrade selection arm and reflection_gain to indistinguishable-from-MIPROv2 levels (predictions: ablated hit_rate<0.30, binomial_p>0.10, vs_mipro_delta<0.005). Actual result: ablation does degrade but RETAINS most of the signature — ablated_hit_rate=0.50 (vs full 0.90), ablated_binomial_p=6.9e-17 (still 17 orders of magnitude below uniform), ablated_vs_mipro_delta=0.029 with paired t-test p=1.15e-05 and 95% CI [0.020, 0.040] strictly above zero. Zero of three strong predictions hit. Mechanism update: metadata-bias is a contributory amplifier (raises full_top5_ic from 0.039 to 0.082, ~2.1× lift), NOT the load-bearing piece. The leaf-seeded structural proposer + Pareto reflection retain the bulk of the dual-arm signature even with uniform field sampling. The full pipeline still clears all 5 success criteria as in draft02 (full_top5_ic=0.082>0.060, hit_rate=0.90, delta=0.072 p=2.9e-07), so the parent claim is not undermined — only the specific 'metadata-bias is necessary' sub-hypothesis is refuted. Pruning this branch with the substantive lesson recorded; downstream nodes should treat leaf-seeded structural proposer (NOT metadata-bias) as the load-bearing mechanism on the cross-sectional axis.
