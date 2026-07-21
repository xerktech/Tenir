import "@testing-library/jest-dom/vitest";

// jsdom's built-in localStorage stub does not implement .clear(), even with a
// concrete origin set via vite.config.ts `environmentOptions.jsdom.url`.
// Provide a minimal in-memory replacement so theme.test.ts beforeEach/afterEach
// can call localStorage.clear() reliably.
class InMemoryStorage implements Storage {
  private data = new Map<string, string>();
  get length() { return this.data.size; }
  clear() { this.data.clear(); }
  getItem(key: string) { return this.data.get(key) ?? null; }
  key(index: number) { return Array.from(this.data.keys())[index] ?? null; }
  removeItem(key: string) { this.data.delete(key); }
  setItem(key: string, value: string) { this.data.set(key, String(value)); }
  [key: string]: any;
  [index: number]: string;
}

Object.defineProperty(window, "localStorage", {
  value: new InMemoryStorage(),
  writable: true,
  configurable: true,
});
