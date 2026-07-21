import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    // Concrete (non-opaque) origin so jsdom's localStorage doesn't throw a
    // SecurityError; tests/setup.ts then supplies a Storage with .clear()/.removeItem().
    environmentOptions: { jsdom: { url: "https://localhost/" } },
    setupFiles: ["./tests/setup.ts"],
    globals: true,
  },
});
