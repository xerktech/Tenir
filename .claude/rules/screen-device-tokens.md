---
paths:
  - "api/src/api/auth/deps.py"
  - "api/src/api/screen/ws.py"
  - "api/tests/test_device_tokens.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_pg_users_live.py"
---

# Screen device tokens

- Device tokens (`scope='screen'`) must stay refused everywhere but `/ws/screen`:
  `principal_from_live_token` compares the scope exactly, and `renew_token_if_due` refuses a
  scoped token (it would otherwise slide forever). The test renews at 60% of the token's life;
  a shorter offset can't fail. Test:
  `test_device_token_is_minted_for_the_caller_and_scoped_to_the_screen_socket`.
- A device token is live only while its `device_tokens` row (id = the token's `jti`, owned by the
  token's user) is unrevoked and unexpired (XERK-2594). `principal_from_live_token` checks it for
  every scoped token; a token without `jti` is refused (unrevocable), never let through.
  - Revoke closes open sockets via `screen.ws.revoke_token`; the handler re-checks the row after
    registering its revoker, as it does for account deletion.
  - A screen socket closes at its token's `exp` (`Principal.expires_at`, scoped tokens only,
    XERK-2612). Without it a mint purging the expired row left the socket unrevocable.
  - Tests: `test_device_tokens.py`; SQL `test_pg_users_live.py::test_device_tokens_round_trip_and_revoke`.
- `/ws/screen` sockets aren't registry sessions: `delete_user` calls `screen.ws.revoke_user`.
  Test: `test_deleting_the_user_closes_their_open_screen_socket`.
