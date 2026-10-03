---
paths:
  - "api/**"
---

# Running the API tests from a worktree

- `import api` may resolve to ANOTHER worktree's editable install (`pip install -e` in a sibling
  checkout), so pytest silently tests that tree's source, not yours.
- Check first: `python -c "import api.session; print(api.session.__file__)"`.
- Pin it per run with `PYTHONPATH=$PWD/src` from `api/` rather than re-installing over a peer's env.
