// Generate a deploy-ready app.json whose `network` whitelist points at the real
// api host(s) instead of the committed localhost dev default. The Even Hub host
// ENFORCES this whitelist for the network permission, so a packed production build
// must list its actual api host(s) or the lens WSS/HTTPS connection is blocked
// and the caption loop never connects (master plan §4.4).
//
//   TENIR_API_HOSTS=api.example.com npm run pack -w tenir-even
//
// Multiple hosts are comma-separated. When unset it falls back to localhost so the
// dev `pack` keeps working unchanged. The committed app.json stays the localhost
// dev default; this writes app.packed.json (gitignored) for `evenhub pack`.
//
// Wildcards (BYO self-hosting): the api URL is a user-editable runtime setting, so
// the wearer points the app at *their own* server — a host we can't enumerate at
// pack time. To allow arbitrary user-supplied hosts, set a wildcard:
//
//   TENIR_API_HOSTS='*'              -> https://*  + wss://*   (any host)
//   TENIR_API_HOSTS='*.example.com'  -> https://*.example.com + wss://*.example.com
//
// A wildcard may be combined with explicit hosts (comma-separated). NOTE: whether
// the Even Hub host actually honours a wildcard entry is enforced by the platform,
// not us — verify the packed manifest is accepted before relying on it.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Build the `network` whitelist (https + wss origins) from a TENIR_API_HOSTS value.
 * Pure + exported so it can be unit-tested without touching the filesystem. Hosts
 * are comma-separated; `*` (or any host containing one) is passed through verbatim
 * to produce a wildcard origin for BYO self-hosting. Falls back to localhost when
 * empty so the dev `pack` keeps working.
 */
export function buildWhitelist(hostsEnv) {
  // Treat unset/blank the same — fall back to localhost so the dev `pack` works.
  const raw = (hostsEnv ?? "").trim() || "localhost";
  const hosts = raw
    .split(",")
    .map((h) => h.trim())
    .filter(Boolean);
  if (hosts.length === 0) throw new Error("TENIR_API_HOSTS resolved to no hosts");
  return hosts.flatMap((h) => [`https://${h}`, `wss://${h}`]);
}

// Only run the file-writing side effects when invoked as a script (not when a test
// imports `buildWhitelist`).
const isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (isMain) {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const app = JSON.parse(readFileSync(join(root, "app.json"), "utf8"));

  const whitelist = buildWhitelist(process.env.TENIR_API_HOSTS);
  const net = app.permissions.find((p) => p.name === "network");
  if (!net) throw new Error("network permission missing from app.json");
  net.whitelist = whitelist;

  const out = join(root, "app.packed.json");
  writeFileSync(out, `${JSON.stringify(app, null, 2)}\n`);
  console.log(`gen-app-json: wrote ${out} (whitelist: ${whitelist.join(", ")})`);
}
