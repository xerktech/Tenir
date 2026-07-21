/**
 * React Native entrypoint. Metro/Hermes load this on device; it registers the root
 * component with the native host. The TypeScript app and its tests live under `src/`.
 *
 * This MUST be the first import: React Native's built-in `URL` implements none of the
 * component getters — `URL#host`, `#pathname`, `#protocol`, `#search` all throw — so
 * any code that parses a URL (the server-URL field, the api-base derivation, history
 * search's `URLSearchParams`) silently fails on device even though it passes under
 * Node in the vitest suite. The polyfill installs a spec-compliant `URL`/
 * `URLSearchParams` globally before `client-core` runs, so it must load before the
 * app entry imported below.
 */
import "react-native-url-polyfill/auto";

import { AppRegistry } from "react-native";

import { name as appName } from "./app.json";
import { App } from "./src/App";

AppRegistry.registerComponent(appName, () => App);
