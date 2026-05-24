# Roadmap

## v0

- Maintain the scaffold and mock end-to-end loop.
- Validate that node contracts, critic routing, rebuttal packets, and AC
  decisions compose cleanly.
- Keep implementation standard-library only.

## v1

- Add real JSON-schema validation.
- Add Claude Code dry-run command generation tests.
- Implement auth preflight checks for subscription OAuth.
- Expand the deterministic local runner from manifest validation to bounded
  execution for safe smoke jobs.
- Add live web baseline resolution behind source snapshot and refresh policy.

## v2

- Add live Claude Code worker backend behind manual smoke gates.
- Add Sakana-v2-compatible staged tree search loop.
- Add failure retrieval and selective fail-file loading. (Initial indexed
  retrieval and non-binding branch priors are implemented; richer weighting
  remains open.)
- Add lesson distillation maintenance.

## v3

- Add publication renderers for interactive HTML, HTML slides, markdown paper,
  and optional final TeX. (Interactive HTML, HTML slides, and markdown paper
  are implemented behind the AC-accept publish dispatcher; TeX still optional
  and off.)
- Add calibrated rebuttal scoring and AC policy.
- Add richer tree visualization and artifact drilldown.
