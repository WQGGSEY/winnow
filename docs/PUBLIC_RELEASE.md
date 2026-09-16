# Public-release checklist

This is a checklist, **not a completed security audit, license clearance, or
statement of eligibility for a support program**. Do not change visibility until
the maintainer has reviewed the release boundary. Opening/merging a PR does not
itself perform that review or publish the repository.

## 1. Rights and disclosure review

- [ ] Approve the proposed MIT license for original work and confirm the right
      to license it. Review copied/adapted code, instruction files, templates,
      arXiv metadata, vendored assets, and any third-party datasets separately.
      Preserve source terms; see `THIRD_PARTY_NOTICES.md`.
- [ ] Inspect all branches/tags and historical content for credentials, private
      data, local machine details, unpublished work, and redistribution limits.
      Include `.audit/`, `docs/research/`, `research_profile.md`, `lessons.yaml`,
      commit messages/emails, issue/PR attachments, and Actions logs.
- [ ] Use a history-aware secret scanner and manually review its findings in a
      private location. Rotate/revoke exposed secrets first. A clean scan is
      not proof of absence; do not publish raw secret findings.
- [ ] Decide whether any historical content must be removed before visibility
      changes. A normal PR cannot erase historical commits or existing copies.

The initial preparation PR only reviews selected current files and removes
machine-bound resource defaults. It must not be described as a full source,
Git-history, dependency, or security audit.

## 2. Preserve operator configuration

- [ ] Before merging portable defaults, back up the current `settings.json`
      outside the repository. Reapply only necessary private resource settings
      to the local checkout after upgrading.
- [ ] Verify the selected Python executables, installed capabilities, data
      adapter entries, MCP environment, and model availability on this host.
      Do not copy private configuration back into a public commit.

The public default uses `PATH`, advertises local CPU execution, and registers
no private datasets. It does not delete existing environments/data, change
model selections, or loosen research/evidence gates. `settings.local.json`
remains UI state; it is not a general configuration overlay.

## 3. Validate the candidate commit

- [ ] Follow README installation in a clean Linux checkout and pass the
      bubblewrap namespace probe without bypassing isolation.
- [ ] Run `python -B -m pytest -ra` and
      `python -B -m research_harness.local_preflight` without provider
      credentials or live acknowledgements. Record the commit, Python/OS,
      dependencies, pass/fail/skip counts, and known limitations.
- [ ] Build a wheel and verify license notices, schema files, and UI assets.
      A wheel build does not establish standalone runtime operation: repository
      configuration/personas are still needed.
- [ ] Review the actual CI checks for this PR/commit, not historical test counts
      in commit messages. Separate CI, mocked tests, local experiments, live
      model calls, and full scientific validation in every report.
- [ ] Document genuine unresolved failures rather than hiding them through
      selective test exclusions, weakened assertions, or fabricated results.

The workflow uses hosted Linux runners, read-only repository permissions,
SHA-pinned actions, and no supplied model-provider secrets. Running it still
consumes GitHub Actions resources. It neither publishes packages nor changes
repository settings.

## 4. Publish and describe accurately

- [ ] Enable private vulnerability reporting when the repository settings allow
      it and verify that the Security tab exposes the reporting route.
- [ ] Review the README's maintained-prototype status and limitations. Do not
      claim adoption, successful autonomous research, or production readiness
      without evidence.
- [ ] Change repository visibility manually only after the preceding review.
      Verify anonymous access to the README, license, source, and relevant CI.
- [ ] For Codex for OSS, describe the real maintainer role and planned
      maintenance work. Use current official criteria; do not manufacture
      users, stars, test results, releases, or promised API usage.

Official program information:
https://developers.openai.com/community/codex-for-oss

Public visibility and a license make the project inspectable; they do not
prove ecosystem impact or guarantee program acceptance.
