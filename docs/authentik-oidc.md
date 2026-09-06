# Authentik OIDC for Tenir (XERK-647)

Authentik is the household's **optional** identity provider. It runs as its own
stack (`authentik/docker-compose.authentik.yml`) — separate from the root Tenir
stack — with its own Postgres + Redis, and is managed as a Portainer stack on the
hub host. This document is the handoff for the rest of epic XERK-646: it records
every config value the downstream tickets (T2/T3/T7–T10) consume, plus how to
deploy and verify.

No Tenir code lives here. All Authentik config is declarative in
[`authentik/blueprints/tenir-oidc.yaml`](../authentik/blueprints/tenir-oidc.yaml),
applied by the Authentik worker on boot — there is no manual UI click-ops. To
change the provider, application, groups, users or claim mappings, edit that
blueprint and restart the worker; entries are matched by their identifiers, so
re-application is idempotent.

## What the blueprint creates

| Object | Value |
|---|---|
| OAuth2/OIDC provider | **Tenir** — public client + PKCE, RS256 signing |
| Application | **Tenir** (slug `tenir`) |
| Groups | `tenir-admins` (root/admin), `tenir-members` |
| Users | `tenir-root` ∈ `tenir-admins`, `tenir-member` ∈ `tenir-members` — both with verified emails |
| Scopes/claims | `openid`, `profile`, `email` (built-in), `groups` (custom) |

The root user's email is set from `AUTHENTIK_TENIR_ROOT_EMAIL`, which **must equal
the API's `API_AUTH_ADMIN_EMAIL`** so T4 links the OIDC identity to the local
env-admin on first login. Both seeded users carry `email_verified: true`.

## Config values for downstream tickets (T2/T3/T7)

Let `AUTHENTIK_URL` be the externally-reachable base URL of Authentik on the hub
(e.g. `https://auth.<household-domain>`, or `http://<hub>:9000` for a bare
deploy). Authentik uses **per-provider** issuers, so every OIDC URL is namespaced
under the application slug `tenir`:

| Value | What to use |
|---|---|
| **Issuer** (`iss`) | `AUTHENTIK_URL/application/o/tenir/` |
| **Discovery** | `AUTHENTIK_URL/application/o/tenir/.well-known/openid-configuration` |
| **JWKS URL** | `AUTHENTIK_URL/application/o/tenir/jwks/` |
| **Authorize endpoint** | `AUTHENTIK_URL/application/o/authorize/` |
| **Token endpoint** | `AUTHENTIK_URL/application/o/token/` |
| **Userinfo endpoint** | `AUTHENTIK_URL/application/o/userinfo/` |
| **End-session endpoint** | `AUTHENTIK_URL/application/o/tenir/end-session/` |
| **Client ID** | `AUTHENTIK_TENIR_CLIENT_ID` (default `tenir`) — same id for all three fronts |
| **Client type** | public (PKCE, no secret). T2 confirms if any front is confidential instead. |
| **Audience** (`aud`) | the client id (`tenir`) |
| **Signing alg** | RS256 (keys at the JWKS URL above) |
| **Group claim** | `groups` — array of group names, e.g. `["tenir-admins"]` |
| **Email claims** | `email` (string), `email_verified` (bool) |
| **Subject claim** | `sub` — stable per user (hashed user id) |
| **Scopes to request** | `openid profile email groups` |

Redirect URIs are configured from env and default to placeholders; **T7–T10
finalize the real values**:

| Client | Env var | Placeholder |
|---|---|---|
| Web (served by API) | `TENIR_OIDC_REDIRECT_WEB` | `http://localhost:8080/auth/oidc/callback` |
| even phone page | `TENIR_OIDC_REDIRECT_EVEN` | `http://localhost:8080/even/auth/oidc/callback` |
| mobile native | `TENIR_OIDC_REDIRECT_MOBILE` | `com.xerktech.tenir://auth/callback` |

## Deploy on the hub (Portainer)

Per repo ops: this is a Portainer-managed stack; no host log spelunking.

1. Create the `.env` on the hub from `.env.example` — fill the Authentik section.
   Generate secrets: `AUTHENTIK_SECRET_KEY` = `openssl rand -base64 60`, and
   strong values for the two Postgres/bootstrap passwords and the two seeded-user
   passwords. Set `AUTHENTIK_TENIR_ROOT_EMAIL` to the API's `API_AUTH_ADMIN_EMAIL`.
   Finalize secrets in T11.
2. In Portainer → Stacks → Add stack, point it at
   `authentik/docker-compose.authentik.yml` (git or upload), supply the env, deploy.
3. Set `AUTHENTIK_BIND`/reverse-proxy so `AUTHENTIK_URL` resolves with TLS. The
   Postgres volume `authentik-postgresql` persists all state across restarts.
4. First boot: the worker applies the blueprint. The built-in `akadmin` superuser
   (for administering Authentik itself) uses `AUTHENTIK_BOOTSTRAP_PASSWORD`.

## First-boot ordering

The Tenir blueprint `!Find`s objects that Authentik provisions from its *own*
default/system blueprints and a post-migrate task: the two default flows
(`default-provider-authorization-explicit-consent`,
`default-provider-invalidation-flow`) and the auto-generated
`authentik Self-signed Certificate` (the RS256 signing key). On a cold first
boot the Tenir blueprint may apply *before* those exist, in which case the
provider entry fails validation — Authentik **logs** this and moves on (it does
not crash), then re-applies the blueprint on its next reconciliation pass once
the dependencies are present. So on a fresh stack, confirm steady state rather
than trusting the first apply: after ~1–2 minutes, re-check the worker logs and
the API (below) and confirm the provider/app now exist with no lingering error.
A `docker compose ... restart worker` forces an immediate re-apply.

## Verify (acceptance criteria)

```bash
# Discovery + JWKS resolve
curl -fsS "$AUTHENTIK_URL/application/o/tenir/.well-known/openid-configuration" | jq .
curl -fsS "$AUTHENTIK_URL/application/o/tenir/jwks/" | jq '.keys[0].alg'   # -> "RS256"

# Tokens carry sub, groups and a verified email. Drive the authorization-code
# + PKCE flow (or the token endpoint) as tenir-root / tenir-member, then decode:
#   id_token payload MUST contain: sub, email, email_verified=true,
#   groups=["tenir-admins"]  (root)  /  ["tenir-members"]  (member)
```

Survives a restart: `docker compose -f authentik/docker-compose.authentik.yml
restart` (or a Portainer redeploy) — the provider, app, groups and users are all
still present because state lives in the persisted Postgres volume and the
blueprint is idempotent.
