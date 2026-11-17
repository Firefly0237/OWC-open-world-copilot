# Security policy

## Reporting a vulnerability

Please use GitHub's private **Report a vulnerability** flow for this repository. If that control is
not enabled, open a non-sensitive issue titled `Security contact request` so the owner can arrange a
private channel; include no exploit detail there. Do not publish a secret, personal data, or an
unredacted production log in an issue or pull request.

Include the affected version/commit, deployment profile, impact, minimal reproduction, and any safe
mitigation. Maintainers will validate scope and coordinate disclosure; no response-time or release
SLA is promised.

## Supported surface

Security fixes target the current default branch and, when published, the latest release. Older
commits and local forks are not maintained. Local mode is designed for a trusted workstation. Any
network-exposed or real-model service must set `OWCOPILOT_API_KEY`, keep provider credentials outside
the repository, restrict ingress, and use operator-managed secrets. The default entry point is the
local workbench; the retained team backend requires a separate deployment and security review.

Rotate any credential that has appeared in a commit, log, screenshot, shell history, or generated
artifact. Deleting the visible file is not sufficient because Git history and caches may retain it.
