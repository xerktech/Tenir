/**
 * Locate the Veiller miniapp simulator.
 *
 * The simulator lives in the Veiller monorepo (`sdk/miniapp-simulator`) because
 * it emulates the phone, not this miniapp. Tenir is a separate repo, so point
 * `VEILLER_REPO` at a Veiller checkout — or keep the two side by side, which is
 * the default assumed here.
 */

import {existsSync} from "node:fs"
import {resolve} from "node:path"

const ENTRY = "sdk/miniapp-simulator/src/index.ts"

export function simulatorEntry(): string {
  const roots = [
    process.env.VEILLER_REPO,
    resolve(import.meta.dir, "../../../Veiller"),
    resolve(import.meta.dir, "../../../veiller"),
  ].filter((r): r is string => Boolean(r))

  for (const root of roots) {
    const candidate = resolve(root, ENTRY)
    if (existsSync(candidate)) return candidate
  }

  throw new Error(
    `Could not find the Veiller miniapp simulator (${ENTRY}).\n` +
      `Set VEILLER_REPO to a Veiller checkout, e.g.\n` +
      `  VEILLER_REPO=~/git/Veiller bun run sim/walkthrough.ts\n` +
      `Looked in: ${roots.join(", ")}`,
  )
}

/**
 * The slice of the simulator's surface this repo's scripts use. Declared
 * structurally because the module is loaded by path from a sibling checkout,
 * so there is no package to import types from.
 */
export interface SimulatorModule {
  Simulator: new (opts: {
    bundle: string
    model?: string
    userId?: string
    storage?: Record<string, string>
    verbose?: boolean
  }) => SimulatorInstance
  delay: (ms: number) => Promise<void>
  silencePcm: (ms: number) => string
}

export interface SimulatorInstance {
  start(): Promise<void>
  stop(): Promise<void>
  tap(): boolean
  doubleTap(): boolean
  swipeUp(): boolean
  swipeDown(): boolean
  speak(opts?: {base64?: string; ms?: number; format?: string}): boolean
  background(): void
  foreground(): void
  emit(streamType: string, data: unknown): boolean
  lens(view?: "main" | "dashboard"): string
  lensSvg(view?: "main" | "dashboard"): string
  lensText(view?: "main" | "dashboard"): string[]
  settle(quietMs?: number, timeoutMs?: number): Promise<void>
  waitFor(predicate: () => boolean, timeoutMs?: number, message?: string): Promise<void>
  waitForLens(substring: string, timeoutMs?: number): Promise<void>
  phone: {
    open(): void
    close(): void
    send(channel: string, payload?: unknown): void
    request<T = unknown>(channel: string, payload?: unknown, timeoutMs?: number): Promise<T>
    on(channel: string, cb: (payload: unknown) => void): () => void
    last<T = unknown>(channel: string): T | undefined
    waitFor<T = unknown>(channel: string, predicate?: (p: T) => boolean, timeoutMs?: number): Promise<T>
  }
  host: {
    activeSubscriptions(): string[]
    storageSnapshot(): Record<string, string>
    unimplemented: string[]
  }
  glasses: {
    currentRevision(): number
    lens(view?: "main" | "dashboard"): {box: {x: number; y: number; w: number; h: number}; style?: unknown}[]
    model: {scene: {width: number; height: number}}
  }
}

export async function simulatorModule(): Promise<SimulatorModule> {
  return (await import(simulatorEntry())) as unknown as SimulatorModule
}
