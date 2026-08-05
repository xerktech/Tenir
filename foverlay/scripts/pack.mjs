// Pack the built miniapp into a FLAT zip: entries sit at the zip root
// (miniapp.json, background/index.js, ui/index.html, ...), which is the
// layout the Foverlay app expects for bundled miniapps in
// mobile/assets/miniapps/.
//
// Usage: node scripts/pack.mjs [--version X.Y.Z] [--out path/to/file.zip]
//   --version overrides the version read from miniapp.json (name only; the
//             miniapp.json inside the zip is whatever is on disk — CI stamps
//             it before building).
//   --out     overrides the output path (default build/<packageName>-<version>.zip).
//
// Zips with the system `zip -r` when available (CI's ubuntu-latest has it),
// otherwise falls back to python3's zipfile module (deflate).

import {spawnSync} from "node:child_process"
import {copyFileSync, existsSync, mkdirSync, readFileSync} from "node:fs"
import path from "node:path"
import process from "node:process"
import {fileURLToPath} from "node:url"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const distDir = path.join(root, "dist")

function parseArgs(argv) {
  const opts = {version: null, out: null}
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === "--version") {
      opts.version = argv[++i]
    } else if (arg === "--out") {
      opts.out = argv[++i]
    } else if (arg === "--") {
      // ignore separator (e.g. `bun run pack -- --out ...` may forward it)
    } else {
      console.error(`pack.mjs: unknown argument ${arg}`)
      process.exit(1)
    }
  }
  if (opts.version === undefined || opts.out === undefined) {
    console.error("pack.mjs: --version and --out require a value")
    process.exit(1)
  }
  return opts
}

const opts = parseArgs(process.argv.slice(2))

const manifestPath = path.join(root, "miniapp.json")
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"))
const packageName = manifest.packageName
const version = opts.version ?? manifest.version
if (!packageName || !version) {
  console.error("pack.mjs: miniapp.json must have packageName and version")
  process.exit(1)
}

// The build must have run first (bun run build -> dist/).
for (const required of ["background/index.js", "ui/index.html"]) {
  if (!existsSync(path.join(distDir, required))) {
    console.error(`pack.mjs: dist/${required} not found — run \`bun run build\` first`)
    process.exit(1)
  }
}

// The manifest and icon ship at the zip root alongside the bundles.
copyFileSync(manifestPath, path.join(distDir, "miniapp.json"))
copyFileSync(path.join(root, "icon.png"), path.join(distDir, "icon.png"))

const outPath = path.resolve(root, opts.out ?? path.join("build", `${packageName}-${version}.zip`))
mkdirSync(path.dirname(outPath), {recursive: true})

function haveCommand(cmd) {
  return spawnSync(cmd, ["--version"], {stdio: "ignore"}).status === 0
}

// Flat zip: run the archiver from inside dist/ so entries land at the root.
if (haveCommand("zip")) {
  const res = spawnSync("zip", ["-r", "-X", outPath, "."], {cwd: distDir, stdio: "inherit"})
  if (res.status !== 0) process.exit(res.status ?? 1)
} else if (haveCommand("python3")) {
  const py = [
    "import os, sys, zipfile",
    "dist, out = sys.argv[1], sys.argv[2]",
    "zf = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)",
    "for base, _, files in os.walk(dist):",
    "    for f in sorted(files):",
    "        full = os.path.join(base, f)",
    "        zf.write(full, os.path.relpath(full, dist))",
    "zf.close()",
  ].join("\n")
  const res = spawnSync("python3", ["-c", py, distDir, outPath], {stdio: "inherit"})
  if (res.status !== 0) process.exit(res.status ?? 1)
} else {
  console.error("pack.mjs: need either `zip` or `python3` on PATH to create the archive")
  process.exit(1)
}

console.log(`packed ${packageName}@${version} -> ${outPath}`)
