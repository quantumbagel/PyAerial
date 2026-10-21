/**
 * localStorage wrapper that never throws.
 *
 * Private windows, blocked site data, and quota errors all make the Storage
 * accessor (or its methods) throw. Values written during such a session are
 * kept in memory so the UI still behaves consistently until reload.
 */
const memory = new Map<string, string>();

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function readStored(key: string): string | null {
  try {
    const value = storage()?.getItem(key);
    if (value != null) return value;
  } catch {
    // fall through to the in-memory copy
  }
  return memory.get(key) ?? null;
}

export function writeStored(key: string, value: string): void {
  memory.set(key, value);
  try {
    storage()?.setItem(key, value);
  } catch {
    // keep the in-memory copy only
  }
}

export function readStoredJson<T>(key: string): T | null {
  const raw = readStored(key);
  if (raw == null) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/** Test helper: forget in-memory fallbacks. */
export function clearStoredMemory(): void {
  memory.clear();
}
