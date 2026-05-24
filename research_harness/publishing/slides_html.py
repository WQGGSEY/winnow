from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _pre(obj: Any) -> str:
    return html.escape(json.dumps(obj, indent=2))


def _baseline_role_lines(node: dict[str, Any]) -> list[str]:
    return [
        html.escape(item)
        for item in node["claim_contract"]["mandatory_baselines"]
    ]


def _success_criteria_lines(node: dict[str, Any]) -> list[str]:
    return [
        html.escape(item)
        for item in node["claim_contract"]["success_criteria"]
    ]


def _disproof_lines(node: dict[str, Any]) -> list[str]:
    return [
        html.escape(item)
        for item in node["claim_contract"]["disproof_conditions"]
    ]


def _ul(items: list[str]) -> str:
    if not items:
        return "<p>none</p>"
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def render_slides_html(state: dict[str, Any], output_path: Path) -> None:
    node = state["node"]
    reduction = state["orchestrator_reduction"]
    ac_decision = state["ac_decision"]
    worker_report = state["worker_report"]
    title = "Research Harness Slides"
    decision = ac_decision["decision"]
    decision_color = "#0f766e" if decision == "accept" else "#9a3412"
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #0b1220;
      --panel: #111c30;
      --ink: #f1f5f9;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --line: #1e293b;
      --decision: {decision_color};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    .deck {{
      display: grid;
      gap: 24px;
      padding: 32px;
      max-width: 1100px;
      margin: 0 auto;
    }}
    section.slide {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 28px 32px;
      box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset;
    }}
    .slide-number {{
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .slide h2 {{
      margin: 6px 0 14px;
      font-size: 24px;
    }}
    .slide p, .slide li {{
      color: var(--ink);
    }}
    .slide ul {{
      padding-left: 22px;
      margin: 0 0 8px;
    }}
    .pill {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .decision-pill {{
      background: rgba(15, 118, 110, 0.15);
      color: var(--decision);
    }}
    pre {{
      background: #020617;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 14px;
      overflow: auto;
      font-size: 12px;
      color: #cbd5f5;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
    }}
    @media (max-width: 720px) {{
      .deck {{ padding: 18px; }}
      .grid-2 {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="deck">
    <section class="slide">
      <div class="slide-number">Slide 1 / 5</div>
      <h2>{html.escape(title)}</h2>
      <p>{html.escape(node["claim_contract"]["claim_under_test"])}</p>
      <p class="meta">Node {html.escape(node["id"])} &middot; domain {html.escape(node.get("domain", "n/a"))} &middot; stage {html.escape(node.get("stage", "n/a"))}</p>
      <span class="pill">{html.escape(reduction["research_status"])}</span>
    </section>

    <section class="slide">
      <div class="slide-number">Slide 2 / 5</div>
      <h2>Claim Contract</h2>
      <div class="grid-2">
        <div>
          <p class="meta">Mandatory Baselines</p>
          {_ul(_baseline_role_lines(node))}
        </div>
        <div>
          <p class="meta">Success Criteria</p>
          {_ul(_success_criteria_lines(node))}
        </div>
      </div>
      <p class="meta" style="margin-top:14px">Disproof Conditions</p>
      {_ul(_disproof_lines(node))}
    </section>

    <section class="slide">
      <div class="slide-number">Slide 3 / 5</div>
      <h2>Evidence</h2>
      <p>Worker status: <strong>{html.escape(worker_report["status"])}</strong></p>
      <p>Worker verdict candidate: <strong>{html.escape(worker_report["claim_verdict_candidate"])}</strong></p>
      <p>Orchestrator final verdict: <strong>{html.escape(reduction["final_verdict"])}</strong></p>
      <pre>{_pre(worker_report.get("metrics", {}))}</pre>
    </section>

    <section class="slide">
      <div class="slide-number">Slide 4 / 5</div>
      <h2>Critic Governance</h2>
      <p>{len(state["critic_reviews"])} node-level reviews &middot; {len(state.get("rebuttal_critic_reviews", []))} rebuttal-stage reviews via deterministic folder routing.</p>
      <pre>{_pre(state["critic_routing"])}</pre>
    </section>

    <section class="slide">
      <div class="slide-number">Slide 5 / 5</div>
      <h2>AC Decision</h2>
      <span class="pill decision-pill">{html.escape(decision)}</span>
      <p class="meta" style="margin-top:12px">Confidence: {html.escape(ac_decision.get("confidence", "n/a"))}</p>
      <pre>{_pre(ac_decision["score_summary"])}</pre>
    </section>
  </div>
</body>
</html>
"""
    output_path.write_text(body, encoding="utf-8")
