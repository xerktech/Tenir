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

# Disk on the k8x runner pods (XERK-1821)

- Each job gets a fresh per-pod Longhorn scratch volume (ArgoCD `apps/ghar.yaml`, `scratch`) that
  holds dind's `/var/lib/docker` and `_work`; nothing persists between jobs.
  - ENOSPC in a build means that volume is too small — a prune step cannot help. Resize it in ArgoCD.
  - dind's containerd image store keeps compressed layers and extracted snapshots side by side, so
    an image needs ~2x its pull size; the NeMo base of `parakeet-stt` overflowed 60Gi.
  - `df -h /` there is the runner container's root, not Docker's disk; `df "$GITHUB_WORKSPACE"` is.
