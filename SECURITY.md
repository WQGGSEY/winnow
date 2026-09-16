# Security policy

## Reporting a vulnerability

Do not post credentials, private datasets, sensitive run artifacts, or a working
exploit in a public issue. Use GitHub's **Report a vulnerability** link in the
repository Security tab when the maintainer has enabled private reporting.
If that link is absent, open an issue asking only for a private reporting
channel; omit vulnerability details until a private channel is agreed.

Include the commit, environment, affected boundary, minimal reproduction, and
expected versus observed behavior. Redact credentials and personal data.
There is no bug-bounty or guaranteed response-time commitment. Maintenance
currently targets the default branch; older snapshots are not promised fixes.

## Trust boundaries

This is a local, single-operator research prototype, not a hostile multi-tenant
execution platform. The frontend must remain bound to localhost. Do not expose
it through a public reverse proxy or treat access to it as unprivileged.

`LocalRunner` uses bubblewrap to make the host filesystem read-only except for
the assigned workspace and private temporary storage, disable experiment
networking, and hide the evaluation vault. It may expose GPU devices. Critically,
`--ro-bind / /` **does not hide other readable host files**, and a read-only mount
does not remove credentials from the process environment. This is not a claim
of complete sandbox isolation or resistance to malicious code. Use a disposable
host/container with no sensitive readable files or inherited secrets, and keep
the host/kernel and isolation tools updated. Do not disable isolation when a
namespace check fails.

The model CLI, supervisor, acquisition tools, frontend, generated HTML, and local
experiment runner are separate boundaries. Restricting experiment network access
does not mean the whole harness is offline. External text and generated code
can be incorrect or adversarial; inspect resources and use the documented live
execution acknowledgements deliberately. Do not leave unattended runs with
unlimited credentials or assume soft cycle milestones are quota limits.

Evidence validation checks recorded contracts and measurements; it is not proof
that the underlying code, sources, models, or scientific conclusions are sound.

## Before public release

A change to `.gitignore` or deletion at branch HEAD does not remove previous
versions from Git history. Inspect all branches/tags, commit metadata, archived
run material, PR attachments, and Actions logs before changing visibility.
Revoke/rotate a genuinely exposed credential before considering any history
cleanup. Do not publish secret-scanner output containing the secret itself.

The release checklist in [docs/PUBLIC_RELEASE.md](docs/PUBLIC_RELEASE.md) records
these as maintainer tasks. This policy is not a report that a full historical
secret/provenance audit has already passed.
