/**
 * The device-backed OIDC sidecar store (XERK-655), the OIDC counterpart of the token
 * mirror in `storage.ts`.
 *
 * `client-core`'s OIDC flow keeps everything beyond the (shape-agnostic) access token —
 * the refresh token, the access-token expiry, and the short-lived PKCE transaction — in
 * a keyed `OidcStore` (`client-core/src/auth.ts`). Those getters are **synchronous** and
 * read on the hot path (e.g. `getSessionKind`, `oidcTokenDue`), but the device backing
 * store (AsyncStorage / keychain) is async — so, exactly like the token store, this keeps
 * an in-memory mirror for synchronous reads and writes through to the async store in the
 * background.
 *
 * Hydrating the sidecar at startup is what makes **silent refresh survive a relaunch**:
 * without the refresh token + expiry mirrored back in, a returning OIDC user's session
 * reads as a built-in one, no refresh is attempted, and the app bounces to login when the
 * access token expires. Kept React-Native-agnostic (takes a `KeyValueStore`) so it is
 * unit-tested with an in-memory store.
 */

import type { OidcStore } from "@tenir/client-core";

import type { KeyValueStore } from "../storage";

/**
 * The prefix `client-core` uses for every OIDC sidecar key (`tenir.oidc.session`,
 * `tenir.oidc.tx`). We hydrate by prefix rather than by hard-coding those key names so
 * this stays decoupled from `client-core`'s internal constants. The plain bearer token
 * (`tenir.token`) is outside this prefix and owned by the separate token mirror.
 */
const OIDC_KEY_PREFIX = "tenir.oidc.";

export interface MirroredOidcStore {
  /** The synchronous store handed to `client-core` via `configureOidcStore`. */
  store: OidcStore;
  /** Read the persisted OIDC sidecar into memory once at startup; call before rendering. */
  hydrate(): Promise<void>;
}

/**
 * Build a synchronous `OidcStore` mirrored over an async `KeyValueStore`. Reads come from
 * the in-memory mirror; writes update memory immediately and persist in the background.
 * `onError` (optional) surfaces background persistence failures.
 */
export function createMirroredOidcStore(
  kv: KeyValueStore,
  onError?: (err: unknown) => void,
): MirroredOidcStore {
  const mirror = new Map<string, string>();
  const swallow = (err: unknown) => onError?.(err);

  return {
    store: {
      get: (key) => mirror.get(key) ?? null,
      set: (key, value) => {
        mirror.set(key, value);
        kv.setItem(key, value).catch(swallow);
      },
      remove: (key) => {
        mirror.delete(key);
        kv.removeItem(key).catch(swallow);
      },
    },
    async hydrate() {
      if (!kv.getAllKeys) return;
      try {
        const keys = await kv.getAllKeys();
        await Promise.all(
          keys
            .filter((k) => k.startsWith(OIDC_KEY_PREFIX))
            .map(async (k) => {
              const value = await kv.getItem(k);
              if (value !== null) mirror.set(k, value);
            }),
        );
      } catch (err) {
        swallow(err);
      }
    },
  };
}
