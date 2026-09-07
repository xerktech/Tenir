# Optional Authentik OIDC — enablement, migration & rollback runbook

**Epic:** [XERK-646](https://xerktech.atlassian.net/browse/XERK-646) · **This doc:**
[XERK-657](https://xerktech.atlassian.net/browse/XERK-657) (T11). Operator-facing.

This is the "how do I actually turn it on, and how do I turn it back off" guide. Two companion
docs it points at rather than repeats:

- **[`docs/authentik-oidc.md`](authentik-oidc.md)** — deploying the Authentik stack itself and the
  concrete issuer / client-id / JWKS values it publishes.
- **[`docs/auth-oidc.md`](auth-oidc.md)** — the full design contract (token validation, account
  linking, groups→role, ownership). Read it for *why*; read this for *how*.

---

## TL;DR

- **OIDC is opt-in and off by default.** A stock Tenir deployment authenticates with the built-in
  username/password backend exactly as it always has. Nothing here changes that.
- **Enabling it is a config-only change** — a handful of `API_OIDC_*` env vars, no code change, no
  data migration, no schema step you run by hand.
- **It is a *second* backend, not a replacement.** With OIDC on, the API accepts **both** built-in
  bearer tokens **and** Authentik access tokens. The built-in admin can always log in with a
  password even if Authentik is down. Built-in auth is never required to be removed.
- **Disabling it is one line** — set `API_OIDC_ENABLED=false` (or unset it) and restart. Full
  rollback, see [§5](#5-rollback).

---

## 1. Default: built-in auth (do nothing)

This is the shipped behaviour and needs no OIDC config at all.

```bash
cp .env.example .env      # set API_AUTH_SECRET + API_AUTH_ADMIN_USERNAME/_PASSWORD
docker compose up --build # app on :8080
curl -fsS localhost:8080/health
# The public auth advertisement shows built-in only, no OIDC:
curl -fsS localhost:8080/auth/config      # -> {"builtin": true}
```

Clients (web, Android, glasses phone page) show only the username/password form. Every session and
recording is scoped to the logged-in user's household, as today.

## 2. Optionally enabling Authentik OIDC

Enabling OIDC is two independent pieces: **(A)** stand up Authentik, **(B)** point Tenir at it.

### A. Stand up Authentik (once)

Follow **[`docs/authentik-oidc.md`](authentik-oidc.md)** end to end: deploy
`authentik/docker-compose.authentik.yml` as its own Portainer stack, let the worker apply the
`tenir-oidc.yaml` blueprint (provider + application + `tenir-admins`/`tenir-members` groups +
seeded `tenir-root`/`tenir-member` users), and verify discovery + JWKS resolve. That doc is the
source of truth for the concrete **issuer**, **client_id (audience)**, and **JWKS URL** — Authentik
namespaces them under the `tenir` application slug:

| Value | Where it comes from |
|---|---|
| Issuer (`API_OIDC_ISSUER`) | `AUTHENTIK_URL/application/o/tenir/` |
| Audience (`API_OIDC_AUDIENCE`) | the Tenir client_id — `AUTHENTIK_TENIR_CLIENT_ID` (default `tenir`) |
| JWKS (`API_OIDC_JWKS_URL`) | `AUTHENTIK_URL/application/o/tenir/jwks/` — or leave empty to derive it |

**Link the operator's account before first login.** Set `AUTHENTIK_TENIR_ROOT_EMAIL` (Authentik
side) **equal to** `API_AUTH_ADMIN_EMAIL` (Tenir side). Both seeded users carry a verified email;
the matching, verified email is how the operator's Authentik login attaches to their **existing**
local env-admin row — same id, same admin role, same recordings — instead of creating a duplicate
account. This is the one value that must agree across the two stacks.

### B. Point Tenir at it

Add the OIDC block to Tenir's `.env` (the app stack, not the Authentik stack) and restart the
`app` container. Minimum required when `API_OIDC_ENABLED=true`:

```bash
# Tenir app .env — the OIDC block (see .env.example for the full annotated set)
API_AUTH_ADMIN_EMAIL=admin@household.example          # = AUTHENTIK_TENIR_ROOT_EMAIL
API_OIDC_ENABLED=true
API_OIDC_ISSUER=https://auth.household.example/application/o/tenir/
API_OIDC_AUDIENCE=tenir                                # the client_id
# Everything else has a working default that matches the seeded blueprint:
#   API_OIDC_JWKS_URL           empty ⇒ ${issuer}jwks/
#   API_OIDC_GROUPS_CLAIM       groups
#   API_OIDC_ADMIN_GROUP        tenir-admins
#   API_OIDC_MEMBER_GROUP       tenir-members
#   API_OIDC_SCOPES             openid,email,profile,groups
```

```bash
docker compose up -d app          # restart the app with the new env
```

> **Boot guard.** With `API_OIDC_ENABLED=true` the API **refuses to boot** unless
> `API_OIDC_ISSUER` and `API_OIDC_AUDIENCE` are both set (mirrors the `API_AUTH_SECRET` guard). A
> half-configured OIDC that silently accepts nothing never reaches production.

### Redirect URIs per client

The login redirects back to a per-client URI that must be registered in the blueprint (Authentik
rejects an unregistered `redirect_uri`). They live in the **Authentik** stack's env and default to
placeholders — set them to your real hosts:

| Client | Env var (Authentik stack) | Example value |
|---|---|---|
| Web (served by the API) | `TENIR_OIDC_REDIRECT_WEB` | `https://tenir.household.example/auth/oidc/callback` |
| even phone page | `TENIR_OIDC_REDIRECT_EVEN` | `https://tenir.household.example/even/auth/oidc/callback` |
| Android native | `TENIR_OIDC_REDIRECT_MOBILE` | `com.xerktech.tenir://auth/callback` |

Set each to the full callback URL on your real host (the web/even ones should be your public
Tenir origin, i.e. match `TENIR_PUBLIC_URL`, with the callback path shown). The Android
custom-scheme URI is a three-place contract — changing it means changing the app manifest too; see
[`.claude/rules/mobile-oidc.md`](../.claude/rules/mobile-oidc.md).

### Verify (acceptance)

```bash
# 1. The advertisement now includes OIDC (clients show the "Sign in with Authentik" button):
curl -fsS https://tenir.household.example/auth/config | jq
#   -> {"builtin": true, "oidc": {"enabled": true, "issuer": "...", "clientId": "tenir", ...}}

# 2. Built-in still works (IdP-independent): the env-admin logs in with username/password.
# 3. Log in through Authentik as tenir-root  -> lands as admin, sees all household recordings.
# 4. Log in through Authentik as tenir-member -> lands as member, sees only their own recordings.
```

Points (2)–(4) are the ticket's acceptance criteria: built-in unchanged, plus root and member
OIDC login both work.

## 3. Config reference

**Built-in `API_AUTH_*` (always valid — not removed by enabling OIDC):**

| Var | Meaning |
|---|---|
| `API_AUTH_SECRET` | HMAC signing secret for built-in bearer tokens. Required; boot refuses the default. |
| `API_AUTH_ADMIN_USERNAME` / `_PASSWORD` / `_HOUSEHOLD` | Bootstrap admin, reconciled from env on every boot. |
| `API_AUTH_ADMIN_EMAIL` | Env-admin's email = the verified-email link key for their Authentik login. Only meaningful with OIDC on; harmless otherwise. |

**OIDC `API_OIDC_*` (all default off/empty; a stock deploy sets none of them):**

| Var | Default | Meaning |
|---|---|---|
| `API_OIDC_ENABLED` | `false` | Master switch. `false` ⇒ no OIDC code path is reachable. |
| `API_OIDC_ISSUER` | `""` | Required when enabled. `https://<authentik-host>/application/o/tenir/`. |
| `API_OIDC_AUDIENCE` | `""` | Required when enabled. = the Tenir application's client_id. |
| `API_OIDC_JWKS_URL` | derived | RS256 signing keys; empty ⇒ `${issuer}jwks/`. |
| `API_OIDC_GROUPS_CLAIM` | `groups` | Token claim carrying group membership. |
| `API_OIDC_ADMIN_GROUP` | `tenir-admins` | Membership here ⇒ role `admin`. |
| `API_OIDC_MEMBER_GROUP` | `tenir-members` | Membership ⇒ role `member`. **Access gate:** a token in *neither* Tenir group is denied — this is how you revoke a member (see §4). |
| `API_OIDC_SCOPES` | `openid,email,profile,groups` | Scopes advertised to clients via `/auth/config`. |
| `API_OIDC_AUTHORIZATION_ENDPOINT` | `""` | Optional pinned authorize endpoint; empty ⇒ client re-discovers it. |

These are wired through `docker-compose.yml`'s `app` service as `${VAR:-default}` passthroughs, so
setting them in `.env` is all that's needed. The full annotated list is in `.env.example`; the code
defaults are in `api/src/api/config.py`. (Advanced/rarely-changed knobs —
`API_OIDC_ALGORITHMS`, `API_OIDC_EMAIL_CLAIM`, `API_OIDC_EMAIL_VERIFIED_CLAIM`,
`API_OIDC_LEEWAY_SECONDS`, `API_OIDC_ALLOW_USERNAME_LINK` — exist in `config.py` with safe defaults
and are documented there; leave them unset unless you have a specific reason.)

## 4. Optional member migration

Enabling OIDC does **not** require moving anyone. Built-in and OIDC users coexist in the same
`users` table indefinitely. This section is only for an operator who *chooses* to move household
members onto Authentik.

### How linking works (nothing to run)

There is no bulk migration tool and none is needed — accounts link **lazily, on the member's first
Authentik login**, by verified email:

1. Create the member in Authentik (add them to `tenir-members`, or `tenir-admins` for an admin) with
   an **email that matches their existing Tenir local account's email**, and mark it verified.
2. Make sure that email is set on the existing local Tenir row (see the note below — for the
   env-admin it is set from `API_AUTH_ADMIN_EMAIL`; for other members it currently requires a direct
   DB update).
3. The member logs in once through Authentik. The API matches the verified email to their local row
   and **links in place** — the row keeps its **id, role, and every existing recording**. From then
   on that person can log in with **either** their password **or** Authentik.

A member with no matching local row is **JIT-created** as a fresh member on first login — that is
the normal path for a brand-new household member who never had a local account. JIT-creation (and
every OIDC login) requires the user to be in a **Tenir group** (`tenir-admins` or `tenir-members`);
an Authentik user in neither group is denied and gets no Tenir account.

### Revoking an OIDC user

Access for an OIDC user is **governed by Authentik, not by local deletion.** Revoke them by
**removing them from the Tenir group(s) in Authentik** (`tenir-admins` *and* `tenir-members`): their
next access token then carries no Tenir group and the API denies it. To cut access immediately even
within the current token's lifetime, **disable or delete the user in Authentik** — no new token is
issued and the current one expires.

> **The API refuses to locally delete an OIDC account.** `DELETE /auth/users/{id}` returns **409**
> for any row carrying an `oidc_sub`. A local delete would *not* revoke access — a still-valid
> Authentik token would re-provision the account (and an `tenir-admins` user would re-appear as
> admin) on the next request — so revocation must happen in Authentik. (Built-in local-only accounts
> are still deletable and deleting one revokes it at once, as before.) Keep access-token lifetimes
> short in Authentik (§ `docs/auth-oidc.md` §10) so a group removal takes effect promptly.

> **Setting a non-env-admin member's email today.** The env-admin's link email comes from
> `API_AUTH_ADMIN_EMAIL`. For **other** existing local members there is currently **no admin
> endpoint** to set an email (the create-user API takes only username/password/role, and there is no
> update-user endpoint — `api/src/api/auth/router.py`), so linking one of them by verified email
> needs a direct DB write:
>
> ```sql
> -- attach the member's Authentik email to their existing local row so the next
> -- OIDC login links in place instead of JIT-creating a duplicate
> UPDATE users SET email = 'member@household.example'
>     WHERE username = 'member' AND household = 'default';
> ```
>
> Without this, an existing local member who logs in through Authentik is JIT-created as a *separate*
> OIDC-only row (their old local account and its recordings stay untouched under the old login).
> Closing this gap with an admin API is tracked as
> [XERK-661](https://xerktech.atlassian.net/browse/XERK-661).

> **Security guard.** Linking happens **only** when the token's `email_verified` is `true`. An
> unverified email is never a link key (it would be an account-takeover vector). See
> [`docs/auth-oidc.md` §5](auth-oidc.md).

### The ownership backfill (automatic)

Recordings are per-user owned (`conversations.owner`, XERK-651). Rows created **before** ownership
existed have `owner = NULL`. Those are **backfilled to the env-admin automatically** by an
idempotent statement in `schema.sql`, applied on every pool open:

```sql
UPDATE conversations SET owner = (SELECT id FROM users WHERE is_env_admin)
    WHERE owner IS NULL AND EXISTS (SELECT 1 FROM users WHERE is_env_admin);
```

So the household operator keeps every pre-OIDC recording after they link (linking preserves the
local id, and this attributes the legacy rows to that same id). You do **not** run this yourself —
it runs when the app starts against the DB. A `NULL`-owner row is treated as admin-only (never
leaked to a member) until it is backfilled, so nothing is ever orphaned or exposed mid-transition.

If you need a member's *existing* recordings reassigned to them (they were captured under the shared
admin account before the member had their own login), that is a deliberate `UPDATE conversations SET
owner = '<member users.id>' WHERE id = '<conversation id>'` an admin runs by hand — ownership is not
auto-reassigned away from the admin, on purpose.

### Running both side by side

This is the steady state, not a special mode: with OIDC on, some members log in with a password,
some through Authentik, some can do either. No cutover, no flag day. Migrate members one at a time,
at their own pace; the household keeps working throughout.

## 5. Rollback

Turning OIDC off is a single config change and loses nothing:

```bash
# Tenir app .env
API_OIDC_ENABLED=false        # (or remove the OIDC block)
```
```bash
docker compose up -d app      # restart
curl -fsS localhost:8080/auth/config    # -> {"builtin": true}   (OIDC block gone)
```

After this:

- `/auth/config` advertises built-in only; clients drop the Authentik button and show only the
  username/password form.
- **Linked users keep their password login** (the password_hash was never removed by linking), so
  anyone who had a local account before still logs in.
- **JIT-created (OIDC-only) users have no password** and cannot log in while OIDC is off. Their
  recordings are not deleted — they remain owned by that user id and reappear if OIDC is re-enabled.
  There is currently no admin endpoint to set a password on such a row (same gap as the email one
  above); if one of them needs local access while OIDC is off, either re-enable OIDC or recreate the
  account with a password. In practice rollback is safest for a household where every member still
  has their original local password — which is the case unless you deliberately created OIDC-only
  members.
- The `oidc_sub`/`email` columns and the `owner` backfill stay in place; re-enabling OIDC later
  picks up exactly where it left off. There is no destructive step in either direction.

The Authentik stack itself is separate infra — you can leave it running or stop it independently;
Tenir with `API_OIDC_ENABLED=false` never contacts it.
