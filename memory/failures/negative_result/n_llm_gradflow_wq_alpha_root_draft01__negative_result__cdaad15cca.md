---
node_id: n_llm_gradflow_wq_alpha_root_draft01
thread_id: thread_6b352556
category: negative_result
transition: pruned
final_verdict: contradicted_by_low_coverage_oracle
tags: ['automation', 'expression_level_output', 'global_search', 'llm_driven_gradient_flow', 'llm_driven_gradient_flow_alpha_expression_search', 'num_drafts', 'operational', 'overfitting_robustness', 'short_expression_constraint', 'validity']
source: mcp_server
---

# Failure: n_llm_gradflow_wq_alpha_root_draft01

**Claim under test:**
> [operational draft] A multi-layer LLM-driven gradient-flow procedure — where each layer treats an LLM critique of an alpha expression as the 'gradient' (how-to-improve signal) and backpropagates edits through >=3 sequential layers of operator-tree mutations over length-bounded (<=15 operators) WorldQuant Brain expressions — achieves at least +50% higher out-of-sample rank IC and >=2x larger expression-embedding-space coverage than (a) AlphaForgeBench-style single-shot LLM expression generation and (b) genetic-programming expression search at matched LLM/CPU compute budget, on a 3000-stock x 8-year WQ-style panel, while keeping the IS-OOS rank IC gap <= 0.03.

**Professor verdict (pruned):** contradicted_by_low_coverage_oracle

**Headline metrics:** alphaforgebench_coverage=65.9067, budget_per_method=80, coverage_ratio_llm_vs_afb=0.2895, gplearn_coverage=67.72, llm_gradient_best_oos_ic=0.0024, llm_gradient_best_oos_std=0.0134, llm_gradient_coverage=19.08, llm_gradient_is_oos_gap=0.0194, llm_gradient_mean_oos_ic=-0.0009, llm_oracle_mode=deterministic_proxy, n_days=500, n_seeds=5, n_stocks=200, random_coverage=59.6933, z_llm_vs_afb=-2.061, z_llm_vs_gp=-0.0127, z_llm_vs_random=-5.1211

**Lesson (from Professor's response):**

대학원생의 (1)·(2)·(4) 우려가 정확히 데이터로 입증되었습니다. LLM-gradient (deterministic 4-rule oracle) best-OOS-IC=0.0024, random=0.0168, AFB=0.0240, gplearn=0.0025; z_llm_vs_random=-5.12로 통계적으로 random보다 *유의하게 나쁨*. 핵심 원인은 cov_ratio_llm_vs_afb=0.29 — 4-rule oracle (rank-wrap / ts_smooth / neg / leaf-rank-wrap)이 매우 좁은 자손만 생성하여 top-10이 19개 distinct bag만 커버합니다 (AFB는 66개). 이는 global-search 가설을 직접적으로 반증합니다 (4번째 disproof_condition 적중). 이 draft를 prune하고 root에서 second draft로 진행합니다. 다음 draft에서는 oracle을 확장하거나 (보다 다양한 mutation 카탈로그) 진짜 LLM 호출로 대체해야 합니다.
