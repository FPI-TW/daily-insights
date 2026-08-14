import "@testing-library/jest-dom/vitest"
import { beforeEach } from "vitest"

const testLocalStorage = (() => {
  const values = new Map<string, string>()

  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
    clear: () => values.clear(),
    key: (index: number) => [...values.keys()][index] ?? null,
    get length() {
      return values.size
    },
  } satisfies Storage
})()

Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: testLocalStorage,
})
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: testLocalStorage,
})

beforeEach(() => {
  testLocalStorage.clear()
})
