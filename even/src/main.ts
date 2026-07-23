/**
 * tenir — Even Hub app entry.
 *
 * One page, two surfaces (XERK-82): the phone side shows the login page and,
 * once signed in, the app's own phone pages (XERK-93) — Session, a full-page
 * live transcript mirroring the running glasses session (src/phone/session.ts),
 * and History, the stored sessions from the api (src/phone/history.ts) — with
 * the web UI's bottom navigation between them (src/phone/nav.ts). The lens
 * side renders live captions through the SDK bridge. Both run from this single
 * WebView — navigating away would kill the lens app, so nothing here ever
 * navigates.
 *
 * Boot order is strict, and tuned for BLE reality (the Even docs: one flaky hop
 * can hang ~30s, and concurrent bridge calls can crash the connection — which
 * presents as the app closing itself):
 *
 *   1. resolve the bridge (short race; plain browser dev falls through)
 *   2. create the startup page container EXACTLY once, FIRST — before any
 *      storage round-trip — so the lens is never blank while BLE dawdles
 *   3. init config from the DEVICE store (browser localStorage does not
 *      survive restarts in this host); every storage call is timeout-bounded
 *   4. wire events + the phone login page
 *
 * All lens text goes through one serialized LensTextWriter. The session state
 * machine itself — explicit start/stop by click, the "listening" dots + clock
 * ticker, fit-to-band captions (XERK-85) — lives in src/lens/controller.ts.
 * The live loop is: host mic -> PCM -> ApiClient (WSS) -> api -> caption
 * messages -> textContainerUpgrade on the lens.
 */

// Fonts for the phone page, matching the web UI (web/src/main.tsx).
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";

import {
  StartUpPageCreateResult,
  type EvenAppBridge,
  waitForEvenAppBridge,
} from "@evenrealities/even_hub_sdk";

import { wireLens, type LensControls } from "./lens/controller";
import { LensTextWriter, buildStartupContainer, setText } from "./lens/layout";
import { initConfig } from "./config";
import { PhoneHistory, queryPhoneHistoryElements } from "./phone/history";
import { initPhoneLogin, queryPhoneLoginElements } from "./phone/login";
import { initPhoneNav, queryPhoneNavElements, type PhoneNav } from "./phone/nav";
import { SessionPage, querySessionPageElements } from "./phone/session";
import { BridgeStorage, BrowserStorage, withBleTimeout, type KeyValueStorage } from "./state/storage";

// How long to wait for the Even bridge before assuming a plain browser (dev).
const BRIDGE_TIMEOUT_MS = 2000;
// The one-shot startup container gets a longer leash than a routine BLE call —
// without it there is no lens at all.
const STARTUP_CONTAINER_TIMEOUT_MS = 8000;
// App errors (history load/delete failures) toast like the web UI, then fade.
const TOAST_MS = 4000;

/** Show an auto-hiding error toast (the web UI's notify(…, "err")). */
function makeToast(): (message: string) => void {
  const el = document.getElementById("app-toast");
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (message) => {
    if (!el) return;
    el.textContent = message;
    el.classList.add("show");
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => el.classList.remove("show"), TOAST_MS);
  };
}

async function main(): Promise<void> {
  // Race the bridge against a short timeout: packaged app inside the Even
  // Realities WebView -> bridge; plain browser (`npm run dev`) -> null.
  const bridgePromise: Promise<EvenAppBridge | null> = waitForEvenAppBridge().catch((err) => {
    console.warn("tenir: Even bridge unavailable, phone-page-only mode:", err);
    return null;
  });
  const bridge = await Promise.race([
    bridgePromise,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), BRIDGE_TIMEOUT_MS)),
  ]);

  let lens: LensControls | null = null;
  let authed = false;
  const toast = makeToast();

  // The phone pages (XERK-93): History reloads whenever its tab opens; the
  // Session page mirrors the running glasses session — same WebView, so the
  // captions the lens gets are mirrored here with no extra connection.
  const historyEls = queryPhoneHistoryElements();
  const phoneHistory = historyEls ? new PhoneHistory(historyEls, { onError: toast }) : null;
  const navEls = queryPhoneNavElements();
  const nav: PhoneNav | null = navEls
    ? initPhoneNav(navEls, (page) => {
        if (page === "history") phoneHistory?.open();
      })
    : null;
  const sessionEls = querySessionPageElements();
  const sessionPage = sessionEls
    ? new SessionPage(sessionEls, {
        // A session just started on the glasses: surface its live transcript.
        onRecordingStart: () => nav?.show("session"),
      })
    : null;

  // Device path: draw the lens BEFORE any storage round-trip (see file header).
  let writer: LensTextWriter | null = null;
  if (bridge) writer = await createLensSurface(bridge);

  // The device store is the only one that survives app restarts on real
  // glasses; it holds the server URL, bearer token, and cached credentials.
  const storage: KeyValueStorage = bridge ? new BridgeStorage(bridge) : new BrowserStorage();
  await initConfig(storage);

  if (bridge && writer) lens = await wireLens(bridge, storage, writer, sessionPage);

  if (!bridge) {
    // The race timed out but the host may just have been slow: if the bridge
    // shows up late, still bring the lens surface up rather than leaving the
    // glasses dark for the whole run.
    void bridgePromise.then(async (late) => {
      if (!late) return;
      const lateWriter = await createLensSurface(late);
      if (!lateWriter) return;
      lens = await wireLens(late, new BridgeStorage(late), lateWriter, sessionPage);
      if (authed) lens.enable();
    });
  }

  await initPhoneLogin(storage, queryPhoneLoginElements(), {
    onAuthed: () => {
      authed = true;
      // Each sign-in opens on the Session page (the app's front page).
      nav?.show("session");
      lens?.enable();
    },
    onSignedOut: () => {
      authed = false;
      lens?.disable();
      // Drop the loaded history — the next sign-in must not see it.
      phoneHistory?.reset();
      nav?.show("session");
    },
  });
}

/**
 * The one-shot lens layout, first thing after the bridge resolves. Returns the
 * serialized text writer for it, or null when the host refused the container
 * (the phone page still runs).
 */
async function createLensSurface(bridge: EvenAppBridge): Promise<LensTextWriter | null> {
  const result = await withBleTimeout<StartUpPageCreateResult | null>(
    bridge.createStartUpPageContainer(buildStartupContainer()),
    null,
    STARTUP_CONTAINER_TIMEOUT_MS,
  );
  if (result !== StartUpPageCreateResult.success) {
    console.error("createStartUpPageContainer failed:", result);
    return null;
  }
  return new LensTextWriter((container, content) => setText(bridge, container, content));
}

void main().catch((err) => console.error("tenir failed to start:", err));
