/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The web SPA is built into the api container image and served by the api
// itself (same origin). VITE_API_HTTP seeds a different api URL for local dev.
export default defineConfig({
  plugins: [react()],
  server: { port: 5174 },
  build: { target: "es2021", outDir: "dist" },
  test: {
    environment: "jsdom",
    // A concrete (non-opaque) origin so jsdom's localStorage doesn't throw a
    // SecurityError; the in-memory Storage polyfill in tests/setup.ts supplies
    // a `.clear()` that jsdom's stub lacks.
    environmentOptions: { jsdom: { url: "https://localhost/" } },
    globals: true,
    setupFiles: ["./tests/setup.ts"],
  },
});
