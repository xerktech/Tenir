// jsdom's built-in localStorage stub does not implement .clear()/.removeItem()
// reliably (even with a concrete origin set via vitest's environmentOptions.jsdom.url),
// which breaks the auth/token-storage tests that round-trip through localStorage.
// Provide a minimal in-memory replacement so those tests have a real Storage.
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
