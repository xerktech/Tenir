/**
 * The on-device backing store for the token mirror and server URL (master plan §8.5).
 *
 * Uses `@react-native-async-storage/async-storage`, the standard RN key/value store
 * (Keychain on iOS / EncryptedSharedPreferences-class storage on Android via the
 * platform's secure backends). It already matches our `KeyValueStore` shape, so this
 * is a thin re-export plus the seam where a stricter secure store (e.g. Keychain /
 * Keystore via `react-native-keychain`) could be swapped in without touching callers.
 *
 * This module imports the native module and is therefore loaded only at app startup
 * (`bootstrap.ts`), never from unit tests — those inject `memoryKeyValue` instead.
 */

import AsyncStorage from "@react-native-async-storage/async-storage";

import type { KeyValueStore } from "./storage";

/** The device key/value store used to persist the auth token and chosen server URL. */
export function deviceKeyValue(): KeyValueStore {
  return {
    getItem: (key) => AsyncStorage.getItem(key),
    setItem: (key, value) => AsyncStorage.setItem(key, value),
    removeItem: (key) => AsyncStorage.removeItem(key),
  };
}
