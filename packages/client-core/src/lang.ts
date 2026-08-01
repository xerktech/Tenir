/**
 * Human-readable names for the languages the STT engine may report (`Lang` in
 * the contract). Shared across the frontends so a spoken language reads the same
 * everywhere it is surfaced — the web/mobile source-language chips (XERK-160)
 * and the glasses translation box title (XERK-173: "<Source> → English").
 */

import type { Lang } from "@tenir/contract";

export const LANG_NAMES: Record<Lang, string> = {
  en: "English",
  es: "Spanish",
  fr: "French",
  de: "German",
  pt: "Portuguese",
  it: "Italian",
};

/**
 * The display name for a language code, or `undefined` when it is unknown or
 * absent — callers decide their own fallback (a bare code, an untagged line, or
 * a generic title).
 */
export function langName(lang?: string | null): string | undefined {
  if (!lang) return undefined;
  return LANG_NAMES[lang as Lang];
}
