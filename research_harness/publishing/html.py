from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _pre(obj: Any) -> str:
    return html.escape(json.dumps(obj, indent=2))


def render_interactive_html(state: dict[str, Any], output_path: Path) -> None:
    node = state["node"]
    reduction = state["orchestrator_reduction"]
    ac_decision = state["ac_decision"]
    title = "Research Harness Demo"
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --ink: #202124;
      --muted: #5f6368;
      --line: #d7d7d0;
      --panel: #ffffff;
      --accent: #0f766e;
      --warn: #9a3412;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.45;
    }}
    header {{
      padding: 28px 36px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
      gap: 20px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    p {{ margin: 0 0 12px; color: var(--muted); }}
    section, aside {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 3px 10px;
      border-radius: 999px;
      background: #dff3ef;
      color: var(--accent);
      font-weight: 700;
      font-size: 13px;
    }}
    .decision {{
      color: {("#0f766e" if ac_decision["decision"] == "accept" else "#9a3412")};
      font-weight: 800;
      text-transform: uppercase;
    }}
    details {{
      border-top: 1px solid var(--line);
      padding-top: 12px;
      margin-top: 12px;
    }}
    summary {{ cursor: pointer; font-weight: 700; }}
    pre {{
      overflow: auto;
      padding: 12px;
      border-radius: 6px;
      background: #1f2937;
      color: #f9fafb;
      font-size: 12px;
    }}
    @media (max-width: 820px) {{
      main {{ grid-template-columns: 1fr; padding: 14px; }}
      header {{ padding: 20px 18px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p>Mock end-to-end loop for claim contract, bounded worker, critic governance, rebuttal, AC, and publishing.</p>
  </header>
  <main>
    <div>
      <section>
        <h2>Claim</h2>
        <p>{html.escape(node["claim_contract"]["claim_under_test"])}</p>
        <span class="status">{html.escape(reduction["research_status"])}</span>
      </section>
      <section>
        <h2>Evidence</h2>
        <p>Worker verdict: {html.escape(state["worker_report"]["claim_verdict_candidate"])}</p>
        <p>Final verdict: {html.escape(reduction["final_verdict"])}</p>
        <details>
          <summary>Worker Report</summary>
          <pre>{_pre(state["worker_report"])}</pre>
        </details>
      </section>
      <section>
        <h2>Critic Reviews</h2>
        <p>{len(state["critic_reviews"])} node-level critic reviews and {len(state["rebuttal_critic_reviews"])} rebuttal-stage reviews ran through deterministic routing.</p>
        <details>
          <summary>Review Bundle</summary>
          <pre>{_pre(state["critic_reviews"])}</pre>
        </details>
      </section>
      <section>
        <h2>Rebuttal</h2>
        <p>Rebuttal packet and orchestrator rebuttal are generated as markdown artifacts.</p>
        <details>
          <summary>Routing Trace</summary>
          <pre>{_pre(state["critic_routing"])}</pre>
        </details>
      </section>
    </div>
    <aside>
      <h2>AC Decision</h2>
      <p class="decision">{html.escape(ac_decision["decision"])}</p>
      <details open>
        <summary>Scores</summary>
        <pre>{_pre(ac_decision["score_summary"])}</pre>
      </details>
      <details>
        <summary>Full State Bundle</summary>
        <pre>{_pre(state)}</pre>
      </details>
    </aside>
  </main>
</body>
</html>
"""
    output_path.write_text(body, encoding="utf-8")

