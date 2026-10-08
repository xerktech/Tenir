---
paths:
  - "even/src/lens/**"
  - "even/tests/controller.test.ts"
  - "even/tests/stopTeardown.test.ts"
  - "even/tests/authRace.test.ts"
---

# Glasses lens popup box and session teardown

- The popup box (menu, translation, song, cue) is its own host container. Only
  `rebuildPageContainer` removes it; text writes never do. Any path that clears the box
  state must rebuild to the plain page, or the box stays painted over the idle lens.
- `stopSession` rebuilds whenever a box was up, whatever the stop path (menu, phone, api
  error, sign-out). Gating it on "menu was open" left a translation up after a phone stop.
- A failed plain rebuild, or a box rebuild that timed out (it may still land on the host),
  sets `lensPageStale`; the next stop, start or sign-out retries the rebuild.
- Api callbacks run only while their client is the live one (`client === self`), and so
  does the async silent re-login's continuation. A late one reopened a box over the idle
  lens, or started a ghost session with the mic on that nothing could stop.
- `reauthAttempted` resets per started session, never in `connect()`: the heal calls
  `connect()`, and resetting there is an unbounded re-login storm (XERK-236).
- Tests: `even/tests/stopTeardown.test.ts` (host-page model, stop matrix),
  `even/tests/authRace.test.ts`, `even/tests/reauth.test.ts`.
