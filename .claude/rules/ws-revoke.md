---
paths:
  - api/src/api/main.py
  - api/src/api/session.py
  - api/src/api/auth/router.py
  - api/tests/test_ws_deleted_user.py
  - api/tests/test_qa_hardening.py
---

# WS revocation (account delete)

- WS auth runs at the handshake only; `DELETE /auth/users/{id}` revokes registry sessions only.
  `session.start` re-checks the account (before start AND after `registry.register`) so a socket
  with no registered session can't record after its account is gone (XERK-1504).
- A session keeps one disconnect hook per socket bound to it (start + every resume). A resume can
  take over a session while the displaced socket is still open — both must close on revoke.
- Hooks must be safe on a dead socket, idempotent per socket, and dropped in the handler's
  `finally`, or a session resumed in a loop pins every dead socket (unbounded memory).
- `revoke()` iterates a copy of the hook list: a hook may drop itself mid-loop.
- Tests that monkeypatch `main._ws_principal` must name a user that exists in the user store,
  or `session.start` closes 1008. A WS test that waits on a close should first assert on
  something synchronous (log/count) so a regression fails instead of hanging.
- A revoke closes the socket from another task, so the handler can still read a queued frame and
  reply to a closed socket. That RuntimeError (starlette `WebSocketDisconnected`) is a disconnect
  only when the socket state is DISCONNECTED. Never catch it by class alone (XERK-1517).
- Tests: `api/tests/test_ws_deleted_user.py`.
