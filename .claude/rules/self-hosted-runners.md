---
paths:
  - ".github/workflows/**"
  - ".github/actions/**"
---

# Self-hosted (k8x) runners and fork PRs (XERK-1411)

- The repo is public; `[self-hosted, linux]` runners are in-cluster ARC pods with privileged dind.
- Primary control: repo setting Actions → "Fork pull request workflows" =
  **require approval for all external contributors** (`all_external_contributors`).
  - Check: `gh api repos/xerktech/Tenir/actions/permissions/fork-pr-contributor-approval`.
  - Never relax it to `first_time_contributors` — a returning contributor would get the runner.
- Every `pull_request`-triggered job on self-hosted carries
  `if: github.event.pull_request.head.repo.full_name == github.repository`.
  - Defense in depth only: a fork PR runs its own copy of the workflow and can delete the `if`.
  - It stops an approver who skimmed a Dockerfile `RUN` from handing a fork privileged dind.
- Never use `pull_request_target` with a PR-head checkout on self-hosted — that adds secrets too.
