# Tenir design language

How the two Tenir front ends (web SPA in `web/`, React Native Android app in
`mobile/`) are styled, what they share, and which differences are deliberate.
The governing rules live in `CLAUDE.md`: **model the design on the Turma app**
(layout, components, typography, spacing, iconography), **keep Tenir's own
colour scheme** (the teal "Lumen" palette — never Turma's colours), and keep
**web and Android at feature/design parity**.

## Sources of truth

- **Colour tokens**: the CSS custom properties in `web/src/styles.css`
  (`:root` = dark, the signature default; `[data-theme="light"]` = the light
  counterpart). The mobile palettes in `mobile/src/ui/theme.ts` carry the same
  hex values; `mobile/tests/theme.test.ts` parses the CSS and fails when the
  two drift.
- **Structure/idiom**: Turma (`turma/` web dashboard, `android/` app in the
  Turma repo). When adding a component, copy Turma's shape and apply Lumen
  colours.

## Shared conventions (from Turma)

- **Type**: Inter for body, Space Grotesk for display (headings, wordmark) on
  web; display type tracks tight (`letter-spacing: -0.01em`). Micro-labels
  (field/section captions, table sort headers) are ~11–12px, weight 600,
  UPPERCASE, `letter-spacing: 0.04em`. Numerals in tables/clocks use
  `tabular-nums`.
- **Scales**: 4px spacing base; radii 8 (controls) / 11 (toasts) / 14 (cards),
  fully-round pills for badges and nav tabs; 1px borders everywhere, soft
  two-layer shadows only on top-level cards.
- **Nesting**: rows/cards nested inside a surface card step back to the page
  background (two-level background alternation).
- **Buttons**: quiet outlines by default (weight 500), one solid accent
  primary per surface (weight 600), and **arm-then-confirm** destructive
  controls — the first click arms (outline danger fills solid and names the
  commitment, e.g. "Confirm delete"), the second commits, and the armed state
  quietly expires after 4s. No `window.confirm`/dialogs.
  Web: `web/src/ui/ConfirmButton.tsx`; mobile: `ConfirmButton` in
  `mobile/src/ui/components.tsx` over `mobile/src/lib/confirm.ts`.
- **Badges**: fully-round tinted pills built from the semantic colour — 45%
  for the border, 10% for the fill, full strength for the text (Turma's chip
  formula; `color-mix` on web, `withAlpha` on mobile).
- **Status lights**: small dots coloured success/warning/danger; the
  "connecting" state pulses (1.4s opacity loop, reduced-motion-guarded on
  web).
- **Empty states**: centered, muted, dashed-border blocks.
- **Navigation**: web desktop uses header-adjacent **pill tabs** (active =
  accent text on the accent wash); under 760px, and natively on Android, a
  fixed bottom bar of icon-over-label items whose active item carries a 28×3
  top accent indicator. Icon metaphors match across platforms: mic (Live),
  clock (History), ascending bars (Status).
- **Theming**: System / Light / Dark, persisted under the key `tenir.theme`
  on both platforms (localStorage on web, AsyncStorage on Android). Dark is
  the signature default.
- **Toasts**: bottom strip, solid semantic fill (accent ok / danger error),
  4s auto-dismiss.

## Deliberate platform exceptions

Documented per CLAUDE.md ("a deliberate, documented exception rather than an
accidental gap"):

- **System fonts on Android** — no bundled Inter/Space Grotesk. Turma's own
  Android app also ships system type; RN font bundling isn't worth the parity
  gain. Weight/letter-spacing conventions still apply.
- **No glow shadows on Android** — RN has no `box-shadow`; status dots pulse
  but don't glow, and the wordmark dot is flat. (Turma's Android app drops the
  glow too.)
- **No frosted blur on the Android tab bar** — web's bottom bar uses
  `backdrop-filter: blur`; RN would need a native blur dependency, so the bar
  is solid surface.
- **Type scale** — Tenir's web scale (16px base) is larger than Turma's
  (14px); kept for readability on the phone-shaped surfaces Tenir targets.
  Structure (weights, micro-labels, display face) follows Turma.
- **Audio transport** — web uses the native `<audio controls>` element;
  Android uses the custom seek bar (`mobile/src/ui/AudioPlayer.tsx`)
  (XERK-67).
- **Android-only surfaces** — the in-app update banner (XERK-63) and the
  Settings server field (the web SPA is same-origin and needs no server
  picker). Theme choice lives in the web header (toggle) but under Settings →
  Appearance on Android, following platform convention.

## Known feature-parity gaps (not design; tracked as follow-ups)

- Users admin panel: web only.
- History sorting (sortable table columns): web only — mobile shows a plain
  list.
- Pre-capture consent gate: web gates the first capture behind the recording
  notice screen; mobile shows the notice inline on Live (and has a dedicated
  Privacy tab, which web lacks).
- Glasses-mic switching on Live: mobile only.
