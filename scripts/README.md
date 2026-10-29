# scripts

Manual end-to-end smoke test against a **running** stack. Not part of CI (CI
runs the model-free unit suite in `api/`); run it by hand to verify a deployed
stack actually works end to end.

## `functional_test.py`

Drives the stack at `localhost:8080`: auth, the REST surface (`/health`,
`/status`, `/conversations`) and the live WS pipeline
(`session.start` → PCM → `caption.partial`/`caption.final` → `session.end`),
then checks the session persisted and can be deleted.

```bash
docker compose up --build        # from the repo root
TENIR_USERNAME=<admin> TENIR_PASSWORD=<password> python scripts/functional_test.py
```
