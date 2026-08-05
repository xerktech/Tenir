# foverlay — Tenir miniapp for the Foverlay app

The Foverlay bundled-miniapp build of the Tenir Even G2 client (self-hosted
live captions), ported from `even/` in Foverlay's XERK-211. It runs inside the
Foverlay (MentraOS-derived) mobile app: a background bundle drives the glasses
HUD over the miniapp SDK, and a WebView page handles sign-in, the live
transcript, and history.

It is fully self-contained: no workspace deps on `@tenir/*` (the wire protocol
is ported under `src/core/`), and it is deliberately **not** a member of the
root npm workspace — it uses Bun and its own `bun.lock`.

## Layout

- `src/background/` — JSContext entry (glasses HUD, mic streaming)
- `src/ui/` — WebView entry (plain DOM, no framework)
- `src/core/` — Tenir protocol port (auth, WS, config)
- `src/vendor/display-utils/` — vendored text-layout helpers
- `build.ts` — Bun build: `dist/background/index.js` (IIFE) + `dist/ui/`
- `scripts/pack.mjs` — flat-zip packer (see below)
- `vendor/` — prebuilt SDK tarballs (see below)

## Versioning

`package.json` stays at `0.0.0`; the release pipeline stamps the real version
into `miniapp.json` (not `package.json`), the same way `even/app.json` is
stamped. The `miniapp.json` committed here carries a dev-default version.

## Vendored SDK tarballs

`vendor/` holds two `bun pm pack` tarballs from the Foverlay monorepo so this
directory builds without access to that repo's workspace:

- `mentra-miniapp-0.3.0-dev.1.tgz` — the `@mentra/miniapp` SDK with `dist/`
  prebuilt. Its `workspace:*` dependency on `@mentra/cloud-protocol` was
  rewritten to `*` and its `prepare` script stripped (the tarball already
  contains the build output).
- `mentra-cloud-protocol-0.1.0-dev.0.tgz` — the source-only
  `@mentra/cloud-protocol` package, wired in via an npm `overrides` entry.

To regenerate them in the Foverlay monorepo:

1. `cd mobile/modules/miniapp && bun run build && bun pm pack`
2. In the resulting tarball's `package.json`: rewrite the `workspace:*`
   dependency on `@mentra/cloud-protocol` to `*` and delete the `prepare`
   script.
3. `cd cloud-v2/packages/protocol && bun pm pack`
4. Replace the tarballs here, update the `file:` references in `package.json`
   if the versions changed, and re-run `bun install` to refresh `bun.lock`.

## Develop

```sh
cd foverlay
bun install
bun run typecheck   # tsc --noEmit
bun test
bun run build       # -> dist/background/index.js + dist/ui/
bun run pack        # -> build/<packageName>-<version>.zip
```

`pack` produces a FLAT zip (`miniapp.json` at the zip root, next to
`background/` and `ui/`) using the system `zip` when present, else python3's
zipfile. `--version X.Y.Z` and `--out <path>` override the defaults.

## Releasing

The release pipeline (`.github/workflows/release.yml`, `build-foverlay` job)
stamps the release version into `miniapp.json`, builds, packs, and attaches
`tenir-foverlay-v<version>.zip` to the GitHub release. That zip is bundled
into the Foverlay app's `mobile/assets/miniapps/`.
