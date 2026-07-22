import type { KeyValueStorage } from "../src/state/storage";

/** In-memory KeyValueStorage for tests — mirrors BridgeStorage semantics ("" is a miss). */
export class MemStorage implements KeyValueStorage {
  readonly map = new Map<string, string>();

  async get(key: string): Promise<string | null> {
    const raw = this.map.get(key);
    return raw ? raw : null;
  }

  async set(key: string, value: string): Promise<void> {
    this.map.set(key, value);
  }

  async remove(key: string): Promise<void> {
    this.map.delete(key);
  }
}
