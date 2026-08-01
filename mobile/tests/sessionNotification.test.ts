import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import type { LiveCue } from "@tenir/client-core";

import {
  CAPTURING_TEXT,
  CAPTURING_TITLE,
  sessionNotificationContent,
} from "../src/lib/sessionNotification";

const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

const cue = (over: Partial<LiveCue> = {}): LiveCue => ({
  id: "c1",
  title: "Marie Curie",
  body: "Two-time Nobel laureate; pioneered research on radioactivity.",
  ...over,
});

// The notification content is a pure function of the session state (XERK-163) so it can
// be unit-tested without the native module; the RN call that acts on it lives in
// src/native/notification.ts and is only typechecked.
describe("session notification content (XERK-163)", () => {
  it("shows nothing when no session is running", () => {
    expect(sessionNotificationContent(false, null, null)).toBeNull();
    // Even a lingering cue reference is ignored once the session has stopped.
    expect(sessionNotificationContent(false, cue(), 123)).toBeNull();
  });

  it("shows a plain capturing line with no expiry while running with no active cue", () => {
    expect(sessionNotificationContent(true, null, null)).toEqual({
      title: CAPTURING_TITLE,
      text: CAPTURING_TEXT,
      endsAt: 0,
    });
  });

  it("mirrors the active cue's title/body and carries its wall-clock expiry", () => {
    const active = cue({ title: "Radioactivity", body: "Coined the term in 1898." });
    // The cue's expiry rides along so the native side can clear it on time even while
    // the JS release timer is frozen in the background.
    expect(sessionNotificationContent(true, active, 1_000_000)).toEqual({
      title: "Radioactivity",
      text: "Coined the term in 1898.",
      endsAt: 1_000_000,
    });
  });

  it("treats a missing cue expiry as no auto-revert rather than an immediate one", () => {
    // endsAt 0 means the native side keeps the cue until superseded, not that it has
    // already expired — guards against clearing a cue the instant it is shown.
    expect(sessionNotificationContent(true, cue(), null)?.endsAt).toBe(0);
  });
});

describe("session notification wiring (XERK-163)", () => {
  it("posts the notification from useCapture, keyed on the running/active-cue state", () => {
    const useCapture = readText("src/lib/useCapture.ts");
    expect(useCapture).toContain("postSessionNotification");
    // Passes the cue's wall-clock expiry through so the native side can auto-clear it.
    expect(useCapture).toContain(
      "sessionNotificationContent(state.running, state.activeCue, state.activeCueEndsAt)",
    );
    // Keyed on the cue id so it only re-posts when the band changes, not every render.
    expect(useCapture).toContain("state.activeCue?.id");
  });

  it("guards the native call to Android with a running session", () => {
    const native = readText("src/native/notification.ts");
    expect(native).toContain('Platform.OS !== "android"');
    expect(native).toContain("content == null");
    expect(native).toContain("updateNotification");
  });

  it("requests POST_NOTIFICATIONS on Android 13+ without blocking capture", () => {
    const audio = readText("src/audio/native.ts");
    expect(audio).toContain("POST_NOTIFICATIONS");
    expect(audio).toContain("Number(Platform.Version) >= 33");
    // The mic grant is what start() gates on; notification denial must not change it.
    expect(audio).toContain("return granted;");
  });

  it("declares the notification permission and mic service in the manifest", () => {
    const manifest = readText("android/app/src/main/AndroidManifest.xml");
    expect(manifest).toContain("android.permission.POST_NOTIFICATIONS");
    expect(manifest).toContain(".pcmaudio.MicForegroundService");
  });

  it("updates the ongoing notification in place from the native service", () => {
    const service = readText(
      "android/app/src/main/java/com/tenir/pcmaudio/MicForegroundService.kt",
    );
    // A single reused id so cue updates replace, never stack, the foreground notification.
    expect(service).toContain("fun update(context: Context, title: String, text: String, endsAt: Long)");
    expect(service).toContain("notify(NOTIFICATION_ID, buildNotification");
    // Tapping the notification returns to the running session.
    expect(service).toContain("setContentIntent");
    // A late update after stop() must not post a stray notification.
    expect(service).toContain("if (!running) return");
    const module = readText(
      "android/app/src/main/java/com/tenir/pcmaudio/PcmAudioModule.kt",
    );
    expect(module).toContain("fun updateNotification(");
  });

  it("reverts a cue to the capturing line on a native timer, not the frozen JS one", () => {
    const service = readText(
      "android/app/src/main/java/com/tenir/pcmaudio/MicForegroundService.kt",
    );
    // The revert runs on the main looper (alive under the FGS while backgrounded), so a
    // cue clears at its wall-clock expiry even though the JS release timer is frozen.
    expect(service).toContain("Handler(Looper.getMainLooper())");
    expect(service).toContain("handler.postDelayed(revert");
    expect(service).toContain("DEFAULT_TITLE, DEFAULT_TEXT");
    // Each update cancels the prior pending revert; stop() clears it too.
    expect(service).toContain("cancelRevert()");
    // The delay is derived from the wall-clock expiry, floored at zero.
    expect(service).toContain("(endsAt - System.currentTimeMillis()).coerceAtLeast(0L)");
  });
});
