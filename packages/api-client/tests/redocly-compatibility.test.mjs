import { createRequire } from "node:module"
import { dirname, join } from "node:path"
import { describe, expect, it, vi } from "vitest"

const require = createRequire(import.meta.url)
const redoclyRequire = createRequire(require.resolve("openapi-typescript"))
const { readFileFromUrl } = require(
  join(dirname(redoclyRequire.resolve("@redocly/openapi-core")), "utils.js")
)

describe("Redocly patched minimatch compatibility", () => {
  it.each([
    ["https://schemas.example.com/**", true],
    ["schemas.example.com/**", true],
    ["https://other.example.com/**", false],
  ])("matches remote schema headers for %s", async (matches, shouldMatch) => {
    const customFetch = vi.fn(async () => new Response("schema"))

    const result = await readFileFromUrl(
      "https://schemas.example.com/v1/openapi.yaml",
      {
        headers: [{ matches, name: "X-Schema-Token", value: "test-token" }],
        customFetch,
      }
    )

    expect(customFetch).toHaveBeenCalledWith(
      "https://schemas.example.com/v1/openapi.yaml",
      { headers: shouldMatch ? { "X-Schema-Token": "test-token" } : {} }
    )
    expect(result.body).toBe("schema")
  })
})
