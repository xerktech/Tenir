/**
 * Device-backed phone microphone (master plan §10).
 *
 * Wraps the `PcmAudio` native module — a thin Swift/Kotlin recorder that captures the
 * mic at 16 kHz, mono, signed-16-bit little-endian (matching the api STT input so
 * nothing is resampled on the wire) and emits each ~100 ms slice as a base64 string on
 * the `PcmAudio.chunk` event. Live capture keeps recording while the app is backgrounded
 * via the platform's background-audio entitlement (iOS `UIBackgroundModes: audio`) /
 * foreground service (Android), declared in the native projects — see the README.
 *
 * Like `secureStorage.ts`, this imports a native module and so is loaded only on device
 * (never under vitest); the unit tests inject a fake `PcmAudioSource` instead.
 */

import { NativeEventEmitter, NativeModules, PermissionsAndroid, Platform } from "react-native";

import type { PcmAudioSource } from "@tenir/client-core";

/** The native module's surface (implemented in the iOS/Android projects). */
interface PcmAudioNative {
  /** Begin capture; chunks arrive on the `PcmAudio.chunk` event as `{ base64 }`. */
  start(): Promise<boolean>;
  stop(): Promise<void>;
  /** iOS mic-permission prompt (Android uses `PermissionsAndroid`). */
  requestPermission?(): Promise<boolean>;
}

const native = NativeModules.PcmAudio as PcmAudioNative | undefined;

function missing(): never {
  throw new Error(
    "The PcmAudio native module is not linked. Build the iOS/Android app (it ships " +
      "with the app); it is unavailable in JS-only/Expo-Go runtimes.",
  );
}

/** The real, on-device phone-microphone source handed to the capture session. */
export function deviceAudioSource(): PcmAudioSource {
  let chunkSub: { remove(): void } | null = null;

  return {
    async requestPermission() {
      if (Platform.OS === "android") {
        const result = await PermissionsAndroid.request(
          PermissionsAndroid.PERMISSIONS.RECORD_AUDIO,
          {
            title: "Microphone access",
            message: "Tenir needs the microphone to transcribe conversations.",
            buttonPositive: "Allow",
            buttonNegative: "Deny",
          },
        );
        return result === PermissionsAndroid.RESULTS.GRANTED;
      }
      // iOS prompts via the native module (NSMicrophoneUsageDescription in Info.plist).
      return native?.requestPermission ? native.requestPermission() : true;
    },

    async start(onChunk) {
      if (!native) missing();
      const emitter = new NativeEventEmitter(NativeModules.PcmAudio);
      chunkSub?.remove();
      chunkSub = emitter.addListener("PcmAudio.chunk", (e: { base64: string }) => onChunk(e.base64));
      try {
        return await native.start();
      } catch (err) {
        chunkSub.remove();
        chunkSub = null;
        throw err;
      }
    },

    async stop() {
      chunkSub?.remove();
      chunkSub = null;
      await native?.stop();
    },
  };
}
