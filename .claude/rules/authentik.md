---
paths:
  - "authentik/**"
  - "docs/authentik-oidc.md"
---

# Authentik OIDC stack (XERK-647)

- Authentik is Tenir's **optional** IdP and a **separate** stack
  (`authentik/docker-compose.authentik.yml`) with its own Postgres+Redis — never
  fold it into the root `docker-compose.yml`. The root `postgres-tenir` is not
  shared with it.
- All Authentik config is **declarative** in `authentik/blueprints/tenir-oidc.yaml`,
  auto-applied by the worker on boot (mounted at `/blueprints/tenir`). Reconfigure
  by editing the blueprint + restarting the worker — do NOT click-ops in the UI;
  the UI change would be overwritten on the next blueprint apply. Entries are
  idempotent (matched by `identifiers`).
- Blueprint schema is **version-sensitive** — `redirect_uris` as a list of
  `{matching_mode,url}` and `invalidation_flow` require Authentik ≥ 2024.8. Pinned
  via `AUTHENTIK_TAG` (default `2024.12`). Re-test the blueprint when bumping it.
- **First cold boot has an ordering race**: the blueprint `!Find`s the two default
  flows + the self-signed signing cert, all provisioned by Authentik's own
  system blueprints / post-migrate task. If the Tenir blueprint applies first,
  the provider entry fails validation — logged, not fatal — and reconciles on a
  later pass. Verify *steady state* (re-check after ~1–2 min or restart the
  worker), not the first apply. See `docs/authentik-oidc.md`.
- `email_verified` in tokens comes from Authentik's built-in `email` scope
  mapping (hardcodes `true` when the user has an email) — NOT from the user's
  `attributes.email_verified`, which is set only to document intent.
- The seeded `tenir-root` email must equal the API's `API_AUTH_ADMIN_EMAIL` (T4
  links by verified email). Both seeded users set `email_verified: true`.
- Downstream OIDC values (issuer, JWKS, client id, group/email claim names) live
  in `docs/authentik-oidc.md` — the T2/T3/T7 handoff. Keep it in sync with the
  blueprint.
- Secrets come from `.env` (see the Authentik block in `.env.example`); the
  compose `${VAR:?}` guards refuse to start without them.
