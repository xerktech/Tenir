import {DisplayProfile} from "./types"

/**
 * Even Realities G2 Smart Glasses Display Profile
 *
 * Mirrors mobile/modules/engine/src/utils/display/profiles/g2.ts: the G2 uses
 * the same display hardware as the G1 (green monochrome), so glyph widths and
 * the rendering formula are identical to G1. The protocol differs (EvenHub
 * protobuf vs G1 binary) but display characteristics are the same.
 */

// G2 uses the same glyph widths as G1 (same display hardware/font)
import {G1_PROFILE} from "./g1"

export const G2_PROFILE: DisplayProfile = {
  ...G1_PROFILE,
  id: "even-realities-g2",
  name: "Even Realities G2",
  // G2 fits more vertical text than G1 — allow up to 8 lines before the
  // wrapper truncates (G1 stays at its inherited 5).
  maxLines: 8,
  // Hardware-calibrated 2026-07-03 (see the engine profile): a 28px container
  // overflowed one line of the firmware font; 40px is clean.
  lineHeightPx: 40,
}

/**
 * Get the hyphen width for G2 in rendered pixels.
 * Same as G1: Hyphen glyph = 4px → rendered = (4+1)*2 = 10px
 */
export const G2_HYPHEN_WIDTH_PX = 10

/**
 * Get the space width for G2 in rendered pixels.
 * Same as G1: Space glyph = 2px → rendered = (2+1)*2 = 6px
 */
export const G2_SPACE_WIDTH_PX = 6
