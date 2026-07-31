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
    expect(sessionNotificationContent(false, null)).toBeNull();
    // Even a lingering cue reference is ignored once the session has stopped.
    expect(sessionNotificationContent(false, cue())).toBeNull();
  });

  it("shows a plain capturing line while running with no active cue", () => {
    expect(sessionNotificationContent(true, null)).toEqual({
      title: CAPTURING_TITLE,
      text: CAPTURING_TEXT,
    });
  });

  it("mirrors the active cue's title and body so it surfaces in the shade", () => {
    const active = cue({ title: "Radioactivity", body: "Coined the term in 1898." });
    expect(sessionNotificationContent(true, active)).toEqual({
      title: "Radioactivity",
      text: "Coined the term in 1898.",
    });
  });
});

describe("session notification wiring (XERK-163)", () => {
  it("posts the notification from useCapture, keyed on the running/active-cue state", () => {
    const useCapture = readText("src/lib/useCapture.ts");
    expect(useCapture).toContain("postSessionNotification");
    expect(useCapture).toContain(
      "sessionNotificationContent(state.running, state.activeCue)",
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
    expect(service).toContain("fun update(context: Context, title: String, text: String)");
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
});
