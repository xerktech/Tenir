# Tenir — Android app (`mobile/`)

The Android client for a self-hosted Tenir server: **phone-mic live capture**
plus browse/manage of the recorded, stored sessions.

- **Live capture** — stream the phone mic to the server over the same WS
  contract the Even G2 lens uses; partial/final captions render on screen, and
  the finished session is stored on the server (transcript + audio).
- **History** — list/search stored sessions, read the transcript with timing,
  play the retained audio, delete.
- **Status** — per-component server health.
- **Settings** — server URL + account; **Privacy** — the recording disclosure.

Auth is a bearer token from the server login, held in a device-backed store
(`@react-native-async-storage/async-storage`), and the capture session id is
persisted per device so a brief drop or relaunch resumes the same session.

The capture pipeline mirrors the other clients — mic → 16 kHz PCM →
`ApiClient` (WSS) → captions → on-screen render — and is driven by the shared
pure reducer in `@tenir/client-core`, so partial/final captions, pause,
reconnect/resume and per-device session ownership are all unit-tested without a
native runtime.

### Native requirements

Live capture needs a small native module and platform config in the iOS/Android
projects:

- **`PcmAudio` native module** — captures the mic at 16 kHz/mono/s16le and emits each
  ~100 ms slice as a base64 `PcmAudio.chunk` event. `audio/native.ts` is its JS wrapper.
- **Microphone permission** — iOS `NSMicrophoneUsageDescription` in `Info.plist`; Android
  `RECORD_AUDIO` (requested at runtime via `PermissionsAndroid`).
- **Background audio** — so a session keeps streaming when the app is backgrounded: iOS
  `UIBackgroundModes: ["audio"]`; Android a foreground service with a microphone type.

The **Android** project under `android/` implements all of the above: `PcmAudioModule`
(an `AudioRecord`-backed 16 kHz/mono/s16le recorder), `MicForegroundService` (a
`microphone`-typed foreground service that keeps capture alive in the background), and the
`RECORD_AUDIO` / `FOREGROUND_SERVICE*` permissions in `AndroidManifest.xml`. (The iOS
project is still to come.)

## Android build & release

The app is a standard React Native 0.76 (Hermes, legacy architecture) Android project,
wired for this **npm-workspaces monorepo**: `react-native`, AsyncStorage and the shared
`@tenir/*` packages are hoisted to the repo-root `node_modules`, and `metro.config.js` /
`react-native.config.js` / the Gradle `react {}` block all point there. Autolinking uses
the RN settings-plugin default (`npx @react-native-community/cli config` run from `mobile/`),
which resolves the hoisted dependencies correctly.

- **Legacy architecture, no NDK** (`newArchEnabled=false`): the app needs no custom C++ or
  codegen, so the build stays NDK-free, fast and CI-friendly. `minSdk 24`, `compileSdk 35`,
  `targetSdk 34`.
- **Signing**: release builds are signed with an upload keystore when the
  `TENIR_UPLOAD_STORE_FILE` / `…_STORE_PASSWORD` / `…_KEY_ALIAS` / `…_KEY_PASSWORD` Gradle
  properties are provided (e.g. from CI secrets); otherwise they fall back to the debug key
  so the APK is still installable for dev distribution.

### CI pipeline

`.github/workflows/mobile.yml` is the app's single workflow. On a PR that touches the app
or the packages it compiles against it runs typecheck + tests + the type-level build; on a
push to `main` it sets up Node + JDK 17 + the Android SDK, runs the committed Gradle wrapper
(`./gradlew :app:assembleRelease`), and refreshes the rolling `mobile-latest` GitHub
prerelease with the signed APK (`tenir.apk`).

### Building locally

```bash
npm install                       # from the repo root, once
cd mobile/android
./gradlew :app:assembleRelease    # → app/build/outputs/apk/release/app-release.apk
```

The Gradle wrapper (`gradlew` + `gradle/wrapper/gradle-wrapper.jar`) is committed, so
`./gradlew` is self-contained — it downloads Gradle 8.10.2 itself, no global Gradle needed.
For day-to-day dev, `npm run start` (Metro) + `npm run android` from `mobile/` build and
install a debug build.

## Architecture

The screens are thin **presenters** over React-Native-free **container hooks**
(`src/lib/controllers.ts`) built on `client-core` + `useAsync` — so all the data
behaviour is shared with the web SPA in spirit and unit-tested under vitest without a
native runtime. Only the presentational components (`src/screens`, `src/ui`) import
`react-native`; they're covered by `tsc`.

```
src/
  App.tsx            root: bootstrap → auth gating → server bar → tabbed dashboard
  bootstrap.ts       startup wiring (token store + saved server URL); runs on device only
  config.ts          point client-core at a api WS URL; capture defaults
  storage.ts         KeyValueStore + token store + capture session-id store (tested)
  secureStorage.ts   AsyncStorage-backed device key/value (native; not unit-tested)
  audio/             device `PcmAudio` native mic (PcmAudioSource contract from client-core)
  lib/               useAsync, controllers, useCapture, format, toast
  screens/           React Native presenters incl. Live capture (typechecked)
  ui/                small component kit + theme
index.js             React Native entrypoint (AppRegistry)
```

## Develop

From the **repo root** (npm workspaces — install once at the top level):

```bash
npm install
npm run typecheck --workspace tenir-mobile
npm run test --workspace tenir-mobile      # vitest
npm run build --workspace tenir-mobile     # tsc --noEmit
```

Running the app on a device/emulator (Metro + native build) uses the Android project under
`android/` (see **Android build & release** above); the iOS project is still to come. The
PR check (`mobile.yml`) covers typecheck + the shared logic tests + the type-level build,
matching the other frontends, while its push-to-`main` job produces the installable APK.

The shared client/contract live in `packages/client-core` and `packages/contract`;
this app, the Even G2 app (`even/`) and the web SPA (`web/`) all depend on them.
