---
paths:
  - "scripts/**/*.py"
  - ".github/workflows/scripts.yml"
---

# Eval harness module names (XERK-2536)

- Harness tests put their own dir on `sys.path` and import modules by bare name.
- CI runs every harness's tests in ONE pytest process (`scripts.yml`), so two harnesses with
  the same top-level module name collide: the second gets the first one's module.
- Name each harness's modules uniquely, prefixed by harness (`stt_score.py`,
  `longform_score.py`, `public_score.py`) — never a bare `score.py`.
- A new harness with tests joins that single pytest call; never give it its own step.
