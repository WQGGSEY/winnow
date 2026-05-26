---
node_id: n_llm_gradflow_wq_alpha_root
thread_id: thread_6b352556
category: invalid_experiment
transition: needs_child_branch
final_verdict: operational_blocker_branch_to_scoped_children
tags: ['automation', 'expression_level_output', 'global_search', 'llm_driven_gradient_flow', 'llm_driven_gradient_flow_alpha_expression_search', 'overfitting_robustness', 'short_expression_constraint', 'validity']
source: mcp_server
---

# Failure: n_llm_gradflow_wq_alpha_root

**Claim under test:**
> A multi-layer LLM-driven gradient-flow procedure — where each layer treats an LLM critique of an alpha expression as the 'gradient' (how-to-improve signal) and backpropagates edits through >=3 sequential layers of operator-tree mutations over length-bounded (<=15 operators) WorldQuant Brain expressions — achieves at least +50% higher out-of-sample rank IC and >=2x larger expression-embedding-space coverage than (a) AlphaForgeBench-style single-shot LLM expression generation and (b) genetic-programming expression search at matched LLM/CPU compute budget, on a 3000-stock x 8-year WQ-style panel, while keeping the IS-OOS rank IC gap <= 0.03.

**Professor verdict (needs_child_branch):** operational_blocker_branch_to_scoped_children

**Headline metrics:** alphaforgebench_coverage=65.9067, budget_per_method=80, coverage_ratio_llm_vs_afb=0.2895, gplearn_coverage=67.72, llm_gradient_best_oos_ic=0.0024, llm_gradient_best_oos_std=0.0134, llm_gradient_coverage=19.08, llm_gradient_is_oos_gap=0.0194, llm_gradient_mean_oos_ic=-0.0009, llm_oracle_mode=deterministic_proxy, n_days=500, n_seeds=5, n_stocks=200, random_coverage=59.6933, z_llm_vs_afb=-2.061, z_llm_vs_gp=-0.0127, z_llm_vs_random=-5.1211

**Lesson (from Professor's response):**

대학원생의 네 가지 우려를 받아들입니다. 첫 회기 timeout(900s)으로 root의 양적 평가가 불가하여, 클레임을 새 축으로 분해한 child branches로 진행하겠습니다: (1) 운영적 capability — vectorized operators + 축소 panel(200x500)에서 4 method 비교가 실제로 정상 종료되어 best-OOS-IC를 산출하는지를 직접 검증, (2) mechanism — LLM-gradient critique 4종 mutation 중 rank-wrap 단일항의 기여도가 OOS-IC gain의 절반 이상임을 ablation으로 분리, (3) boundary — planted-signal SNR 0.15 환경에서 panel scale (100/200/400 stocks)에 따른 OOS-IC 단조 증가와 천장 효과를 측정. 대학원생이 지적한 'deterministic oracle ≠ LLM' 한계는 (2) mechanism node에서 정면 다루고, 'synthetic prior 일치' 우려는 (3) boundary node에서 SNR 0/낮음/높음 sweep으로 분리합니다.
