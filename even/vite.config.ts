import { defineConfig } from "vite";

// For dev, a proxy can front the api to sidestep CORS (master plan §4.4).
// Point VITE_API_WS at the api WS endpoint; default is localhost:8080.
export default defineConfig({
  server: {
    host: true, // expose on LAN for `evenhub qr` live testing on real glasses
    port: 5173,
  },
  build: {
    target: "es2021",
    outDir: "dist",
    // A single entrypoint (index.html): the phone login/web-UI surface and the
    // lens app share one page — navigating the WebView would unload the lens.
  },
});
