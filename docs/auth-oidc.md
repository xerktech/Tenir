# Optional Authentik OIDC — integration contract (T2)

**Epic:** [XERK-646](https://xerktech.atlassian.net/browse/XERK-646) — Integrate Tenir with
Authentik OIDC · **This doc:** [XERK-648](https://xerktech.atlassian.net/browse/XERK-648) (T2,
Foundation track) · **Blocks:** T3, T4, T5, T6, T7 · **Peer:** T1
([XERK-647](https://xerktech.atlassian.net/browse/XERK-647)) deploys/configures the Authentik side.

This is the shared seam: the contract the API track and the client track both build against so they
stay in parallel without drift. It is a design doc, not code — every "MUST/adds/gains" below is a
statement of the agreed contract for the tickets that implement it, cited to the code as it stands
today.

> **Status of concrete values.** The Authentik instance (T1 / XERK-647) is being deployed but is
> not live yet, so the exact **issuer host**, **client_id (audience)**, and **JWKS host+slug** are
> not settled. This doc pins every *convention, shape, and rule* concretely, and marks the three
> host-specific strings as **`«T1»` placeholders** to be filled from the deployed instance. They
> are read entirely from env (§3), so filling them in is a config change, never a code change. When
> T1 publishes the deployed values, update the table in §3 and the ticket link.

---

## 0. TL;DR — the one-paragraph contract

OIDC is **opt-in and off by default**; a deployment that does not set `API_OIDC_ENABLED=true`
behaves byte-for-byte as it does today. When enabled, the API validates **both** its existing
built-in HMAC bearer tokens **and** Authentik access tokens (JWT, RS256, verified against
Authentik's JWKS) — built-in never stops working, so local admin access survives an IdP outage. Any
token that validates yields the **same `Principal`**, so everything downstream is unchanged. On an
OIDC login the API resolves identity in a fixed order — **`oidc_sub` → verified-email match (link)
→ JIT-create** — with **`email_verified = true` a hard guard on linking**. This is how the
env-admin links into their Authentik account while keeping their id, admin role, and existing
recordings. Role comes from the `groups` claim (`tenir-admins` → admin, else member); the env-admin
stays admin regardless of group. Recordings become **per-user owned** (`conversations.owner` = the
local user id, stable across the local↔OIDC link); members read only their own, admins read all.

---

## 1. Optionality & mode selection

- **Default off, non-breaking.** OIDC is enabled by a single config block (§3), defaulting to
  disabled. With it disabled the auth path is exactly today's: `/auth/login` issues an HMAC bearer
  token (`api/src/api/auth/tokens.py`), `deps.py` resolves the `Principal` from it, and nothing
  about the JWT/JWKS machinery is reachable. **OIDC disabled ⇒ current behavior, unchanged.**
- **When enabled, the API accepts both token kinds** (built-in HMAC **and** Authentik OIDC), not
  OIDC-only. Rationale: a local admin (the env-admin) must be able to log in with
  username/password when Authentik is unreachable — an IdP outage must not lock the household's
  operator out of their own hub. Built-in stays the default backend and is **never required to be
  removed**; there is deliberately no "OIDC-only" mode in this epic.
- **Token-kind discrimination is unambiguous.** The two token shapes cannot be confused:
  - Built-in token = `<base64url-payload>.<base64url-hmac-sig>` — exactly **two** dot-separated
    segments, verified by HMAC-SHA256 with `API_AUTH_SECRET` (`tokens._decode_claims`).
  - OIDC token = a standard JWS — **three** segments (`header.payload.signature`) with a JSON
    header carrying `"alg":"RS256"` and a `"kid"`.
  The validator (T3) inspects the token first: three segments with an RS256 header → OIDC path;
  otherwise → built-in path. There is no algorithm-confusion risk: the built-in verifier only ever
  runs HMAC and the OIDC verifier only ever runs RS256-against-JWKS; neither accepts the other's
  shape, and `alg: none` is rejected outright.

## 2. Coexistence & the users table

Local users and OIDC/linked users live in the **same `users` table** (`schema.sql`). A row is one
of three states, distinguished by which identity columns are populated:

| State | `password_hash` | `oidc_sub` | Can log in with |
|---|---|---|---|
| local-only (today's rows) | set | `NULL` | username/password |
| linked | set | set | either password **or** Authentik |
| oidc-only (JIT-created) | `NULL`¹ | set | Authentik only |

¹ A JIT-created row has no local password. It stays password-less until/unless an admin sets one;
it is not a security downgrade because that row can only ever be reached through a validated
Authentik token.

**Schema additions (implemented by T4/T5, stated here so both tracks code against them):**

```sql
-- users: OIDC identity + email (email is needed for verified-email linking and
-- for the env-admin link key). Both nullable; a local-only row leaves them NULL.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub   TEXT;
-- oidc_sub is the stable Authentik identity: at most one row per subject.
CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_sub_idx ON users (oidc_sub) WHERE oidc_sub IS NOT NULL;
-- Linking matches on verified email; keep it unique so a link target is unambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx    ON users (lower(email)) WHERE email IS NOT NULL;

-- password_hash must become NULLable for oidc-only rows (today it is NOT NULL).
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

-- conversations: per-user ownership (T5). Nullable so legacy rows and the
-- disabled-OIDC path need no value; backfilled to the env-admin id (§9).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS owner TEXT REFERENCES users(id);
CREATE INDEX IF NOT EXISTS conversations_owner_idx ON conversations (household, owner, started_at DESC);
```

The additive `ADD COLUMN IF NOT EXISTS` / `CREATE ... IF NOT EXISTS` pattern matches how the schema
is already applied idempotently on every pool open (see the `translation`/`source` precedents in
`schema.sql`), so an existing data dir gains these columns without a migration tool.

The in-memory `UserStore`/`ConversationStore` (`auth/users.py`, `persistence/conversations.py`) gain
the equivalent fields behind the same Protocols — the OIDC path must work in the memory/stub backend
(CI, simulator) exactly as in Postgres.

## 3. Config block (env)

All OIDC config is env, twelve-factor, under the existing `API_` prefix (`api/src/api/config.py`).
Everything defaults to "off/empty" so an untouched deployment is unaffected.

| Env var | Default | Meaning |
|---|---|---|
| `API_OIDC_ENABLED` | `false` | Master switch. `false` ⇒ no OIDC code path is reachable. |
| `API_OIDC_ISSUER` | `""` | The `iss` the API requires and the base for discovery/JWKS. **`«T1»`** — `https://<authentik-host>/application/o/tenir/` |
| `API_OIDC_AUDIENCE` | `""` | The `aud` the API requires = the Tenir application's **client_id** in Authentik. **`«T1»`** |
| `API_OIDC_JWKS_URL` | derived | RS256 signing keys. Defaults to `{issuer}jwks/`; override only if Authentik is fronted oddly. **`«T1»` host** |
| `API_OIDC_GROUPS_CLAIM` | `groups` | Which token claim carries group membership. |
| `API_OIDC_ADMIN_GROUP` | `tenir-admins` | Membership here ⇒ role `admin`. |
| `API_OIDC_MEMBER_GROUP` | `tenir-members` | Documented member group; presence is **not** required for access (see §7). |
| `API_OIDC_ALLOW_USERNAME_LINK` | `false` | Secondary link key `preferred_username` (§5). **Recommend `false`.** |
| `API_AUTH_ADMIN_EMAIL` | `""` | Email that identifies the env-admin's row, so their Authentik login links to it (§6). |

Discovery: with the issuer known, `{issuer}.well-known/openid-configuration` yields the
`jwks_uri`, `authorization_endpoint`, and `token_endpoint`; T3 may fetch it once at startup instead
of hard-coding, but the JWKS URL default above lets it work without the discovery round-trip.

**Boot validation (T3/T6), mirroring the existing `assert_secure_auth_config`:** when
`API_OIDC_ENABLED=true`, `API_OIDC_ISSUER` and `API_OIDC_AUDIENCE` MUST be non-empty or the API
refuses to boot — a half-configured OIDC that silently accepts nothing (or, worse, skips a check) is
the same class of latent hole `config.py` already guards against elsewhere.

## 4. The token the API validates (OIDC path)

The API validates the **access token** presented as `Authorization: Bearer <jwt>` (and, for the WS,
`?token=<jwt>` — §10), as a **JWT signed RS256**, verified against the Authentik **JWKS**
(`API_OIDC_JWKS_URL`), with keys cached and re-fetched on an unknown `kid`.

Authentik must be configured (T1) to issue an **access token that is a JWT** for the Tenir
application (Authentik's default access token is a JWT signed with the provider's RS256 keypair;
the application's signing key and the `groups`/`email` scopes must be selected). If T1's provider is
set to issue an opaque access token instead, this contract does not hold and T1 must switch it to
the JWT/RS256 form — the API validates the token **locally against JWKS** and does **not** call
Authentik's userinfo/introspection on the hot path.

**Required validations (all MUST pass; any failure ⇒ 401):**

- **Signature** verifies against a JWKS key (RS256; `alg` pinned to `RS256`, `alg: none` rejected).
- **`iss`** exactly equals `API_OIDC_ISSUER`.
- **`aud`** contains `API_OIDC_AUDIENCE` (the client_id). If `aud` is an array, membership; if a
  string, equality.
- **`exp`** in the future; **`nbf`/`iat`** respected with small (≤60s) clock skew.

**Required claims for identity & linking** (a token missing any of these on the OIDC path is a 401
— the API cannot safely place the user without them):

| Claim | Use |
|---|---|
| `sub` | Stable Authentik identity → `users.oidc_sub`. |
| `email` | Link key (§5) and stored on the row. |
| `email_verified` | **Hard linking guard** (§5) — must be boolean `true` to link. |
| `groups` (claim name = `API_OIDC_GROUPS_CLAIM`) | Role derivation (§7). Absent/empty ⇒ member. |

## 5. Account linking (the key decision)

On a validated OIDC token the API resolves the local user row in this **fixed order** and stops at
the first hit:

1. **`oidc_sub` match** — a row already carries this `sub`. Use it. (The steady state after first
   link/JIT.)
2. **Verified-email match → link** — no `oidc_sub` row, but the token's `email` (case-insensitive)
   matches an existing local row's `email`, **and the token's `email_verified` is `true`**. Set
   that row's `oidc_sub` to the token's `sub` (link in place). The row keeps its **id, role, and
   owned recordings** — this is precisely how the env-admin (and any pre-provisioned local user)
   adopts their Authentik identity without a duplicate account.
3. **JIT-create** — no match by sub or by verified email. Create a new row with `oidc_sub`, `email`,
   no `password_hash`, `household` = the single hub household (§8), role from groups (§7).

**Security guard (non-negotiable).** Linking (step 2) happens **only** when `email_verified == true`
in the token. An unverified email is never a link key. Without this guard, anyone who could get
Authentik to mint a token with `email = admin@household` (an unverified attacker-controlled address)
would seize the local admin row — full account takeover. So:

- `email_verified != true` (false, absent, or non-boolean) ⇒ **no link**. Fall through to step 3
  (JIT-create a distinct oidc-only row). The pre-existing local row is untouched.
- `email` absent entirely ⇒ **no link**, JIT-create (but note the token is already rejected at §4
  for missing `email`, so in practice this is the "present but unverified" case).

**Secondary link key — `preferred_username` (decision: NO by default).** `API_OIDC_ALLOW_USERNAME_LINK`
exists but **defaults to `false`, and the recommendation is to leave it off.** `preferred_username`
is mutable in Authentik and carries no verification comparable to `email_verified`, so using it as a
link key reopens the takeover vector that the email-verified guard closes. It is documented as an
opt-in for a deployment that deliberately provisions local usernames to match Authentik usernames
and accepts the weaker guarantee; when on, it is tried **after** verified-email (step 2b), never
before, and never overrides an existing `oidc_sub`.

## 6. Env-admin

- The env-managed bootstrap admin is configured today by `API_AUTH_ADMIN_USERNAME` /
  `_PASSWORD` / `_HOUSEHOLD` and reconciled on every boot by its **stable id**, not its username
  (`auth/users.py::reconcile_admin`). This doc adds **`API_AUTH_ADMIN_EMAIL`**: reconciliation
  writes it onto the env-admin's row so that row is the verified-email link target when the operator
  first logs in through Authentik.
- **Rule: the env-admin is always admin.** After identity resolution, if the resolved row is the
  env-admin row (the `is_env_admin` row, `users_one_env_admin_idx`), its role is forced to `admin`
  **regardless of Authentik group membership** — group-derived role applies to every *other* linked
  user, never to the env-admin. This keeps the household operator in control even if they are left
  out of `tenir-admins` in Authentik, and means the operator can never accidentally lock themselves
  out of admin by an IdP group change.
- The env-admin remains reconciled from env on every boot and cannot be deleted
  (`router.delete_user` already refuses), so linking it to Authentik does not change its lifecycle —
  only how it can authenticate.

## 7. Groups → role

- The **groups claim** is named by `API_OIDC_GROUPS_CLAIM` (default `groups`) and carries an array
  of group names. (T1 configures Authentik to include the `groups` scope so this claim is present.)
- **Mapping:** contains `API_OIDC_ADMIN_GROUP` (`tenir-admins`) ⇒ role **`admin`**; otherwise ⇒
  role **`member`**. `tenir-members` is the documented member group but membership in it is **not**
  a gate — the mapping is "admin group present or not", nothing more.
- **No groups / unknown groups ⇒ `member`.** A token with an absent, empty, or entirely unrecognized
  `groups` claim resolves to a plain member: full use of their **own** data, **no** access to anyone
  else's (ownership in §9 enforces this). There is no "no access at all" state on a token that
  otherwise validated — a validated household member is at least a member.
- **Role is recomputed from the token on every login**, then written to the row. An Authentik group
  change takes effect on the user's next login (subject to token lifetime, §10). The **env-admin is
  the sole exception** (§6).

## 8. Identity & the Principal shape

`Principal` (`api/src/api/auth/tokens.py`) is the single object every REST request and every WS
session is scoped by. It stays a frozen dataclass; the local fields are **unchanged**, and OIDC adds
optional fields that default such that the built-in path constructs the exact same value it does
today.

```python
@dataclass(frozen=True)
class Principal:
    user_id: str                 # unchanged — the LOCAL users.id (see below)
    household: str               # unchanged
    role: Role = "member"        # unchanged
    username: str = ""           # unchanged
    # OIDC-only, all optional so the built-in path is byte-for-byte identical:
    sub: str | None = None       # Authentik subject (oidc_sub) when OIDC-authenticated
    email: str | None = None     # verified email from the token
    groups: tuple[str, ...] = () # raw groups from the token (audit/debug; role already derived)
```

- **`user_id` is always the local `users.id`, never the raw `sub`.** Identity resolution (§5) maps
  the token to a local row *before* a `Principal` is built, so ownership, admin checks, and the
  user-store liveness lookup all key on the same stable local id whether the user logged in locally
  or through Authentik. `sub` is the stable *external* id and lives in the `sub` field / `oidc_sub`
  column; it is not what downstream code keys on.
- **Downstream is unchanged.** `is_admin`, `require_admin`, household tenancy, and
  `principal_from_live_token`'s "does this account still exist?" check (`auth/deps.py`) all operate
  on `user_id`/`role`/`household` exactly as today. The liveness check keeps working for OIDC users:
  after JWT validation the API still looks the local row up by `user_id`, so a user whose local row
  was deleted is 401'd even with an otherwise-valid Authentik token.
- **Household: single hub household.** Tenir is one self-hosted household hub. Every OIDC user
  resolves into the **one** hub household — the env-admin's household (`API_AUTH_ADMIN_HOUSEHOLD`,
  default `default`). OIDC does not introduce multi-household mapping in this epic; a group→household
  scheme, if ever wanted, is a separate future decision and explicitly out of scope here.

## 9. Ownership model (mode-independent)

This is independent of which auth mode authenticated the request — it is about *whose* recording a
conversation is, and it applies identically to local, linked, and OIDC users.

- **`conversations.owner` = the (possibly linked) local `users.id`** of the user whose session
  produced it. Because linking (§5) preserves the local id, a user's recordings stay theirs across
  the local↔OIDC link — no re-owning, no orphaning.
- **Already-available at capture time.** The live session already carries `user_id` from the
  authenticated principal (`Session(..., user_id=principal.user_id)`, `api/src/api/main.py`), so T5
  is "persist the value the session already holds into the new `owner` column", not "thread a new
  value through the stack".
- **Read rules:** a **member** sees only conversations where `owner == self.user_id`; an **admin**
  sees **all** conversations in the household. Enforced in the history/search store layer
  (`persistence/conversations.py` + the SQL backend) and the history router — the same one place
  household tenancy is already enforced, extended with an owner predicate for non-admins.
- **Legacy-row backfill (T5):** existing `conversations` rows predate `owner` and are `NULL`. Backfill
  them to the **env-admin's `users.id`** so the admin keeps every pre-OIDC recording after linking.
  A `NULL` owner is treated as owned-by-admin (visible to admins only) until backfilled, so no
  recording is ever orphaned or leaked to a member during the migration.

## 10. Client flow (optional path)

- **How a client learns OIDC is available — the API advertises it.** There is no feature-advertise
  endpoint today, so this contract adds one: a **public** (unauthenticated, like `/health`)
  **`GET /auth/config`** returning at least:

  ```jsonc
  {
    "builtin": true,                 // username/password form always shown
    "oidc": {                        // present only when API_OIDC_ENABLED
      "enabled": true,
      "issuer":  "https://<authentik-host>/application/o/tenir/",
      "clientId": "<client_id>",
      "authorizationEndpoint": "…",  // from discovery
      "scopes": ["openid", "email", "profile", "groups"]
    }
  }
  ```

  With `oidc` absent/`enabled:false` the client shows only the existing username/password form;
  with it present the client shows the OIDC login button **in addition to** the form. This lives in
  shared `packages/client-core` (T7) so web, mobile, and even consume it identically — the
  cross-platform-parity rule (CLAUDE.md) means the OIDC button, logout, and per-user recording views
  ship on all three fronts in the same effort.

- **Login: Authorization Code + PKCE** (public clients — SPA, React Native, the even phone app —
  hold no client secret). The client redirects to Authentik's `authorization_endpoint` with
  `response_type=code`, `code_challenge` (S256), `scope=openid email profile groups`, exchanges the
  code at the `token_endpoint` for an **access token (the JWT the API validates), an id_token, and a
  refresh token.**
- **Token lifetimes & refresh:** access-token lifetime is set in Authentik (T1; recommend short,
  e.g. 5–60 min) and **refresh is the IdP's job** — the client does **silent refresh** using the
  refresh token against Authentik's `token_endpoint` before expiry. The API's own **sliding renewal
  / `X-Renewed-Token`** (`main.py`) is a **built-in-token mechanism only**: an OIDC JWT does not
  HMAC-decode, so `renew_token_if_due` returns `None` and no renewal header is emitted — the two
  refresh models coexist without special-casing. Clients treat `X-Renewed-Token` as before for
  built-in tokens and ignore it for OIDC sessions.
- **Logout:** clear the local token store (`packages/client-core/src/auth.ts`) as today, and for
  OIDC additionally hit Authentik's `end_session_endpoint` (RP-initiated logout) so the IdP session
  ends too. Built-in logout is unchanged.
- **WS auth is unchanged:** the token still rides `Authorization: Bearer` **or** `?token=<token>`
  (`_ws_principal`, `main.py`) — the browser WebSocket API can't set headers, so `?token=` stays.
  The validator treats an OIDC JWT and a built-in token identically at the handshake; a bad/expired
  token still closes with **1008** after accept, exactly as today.
- **The token store is shape-agnostic.** `TokenStore` holds an opaque string; it does not care
  whether that string is a built-in token or an Authentik access token, so the storage layer
  (localStorage / native keychain) needs no change. The client tracks *which kind* of session is
  active (to know whether to silent-refresh via Authentik) alongside the token.

## 11. What this unblocks (acceptance)

Each of these can now be implemented against this doc with **no open questions** except the three
`«T1»` host strings (§3), which are pure config:

- **T3** — pluggable validation: token-kind discrimination (§1), RS256/JWKS validation and required
  claims (§4), boot validation (§3).
- **T4** — OIDC identity: resolution order + email-verified guard (§5), groups→role (§7),
  env-admin rule (§6), users-table additions (§2).
- **T5** — per-user ownership: `conversations.owner`, read rules, legacy backfill (§9).
- **T6** — backend toggle & coexistence: `API_OIDC_ENABLED`, both-accepted mode, same `Principal`
  out of either path (§1, §8).
- **T7** — client optional OIDC flow: `GET /auth/config` advertise, Auth Code + PKCE, silent
  refresh, logout, WS unchanged (§10) — then T8/T9/T10 render it per platform at parity.

### Open item carried to T1 (XERK-647)

Fill the three `«T1»` values in §3 (`API_OIDC_ISSUER`, `API_OIDC_AUDIENCE`, `API_OIDC_JWKS_URL`
host+slug) from the deployed Authentik instance, and confirm the Tenir provider issues a **JWT
(RS256) access token** with the `email`, `profile`, and `groups` scopes and the `tenir-admins` /
`tenir-members` groups created. No code depends on the exact strings — they are env.
