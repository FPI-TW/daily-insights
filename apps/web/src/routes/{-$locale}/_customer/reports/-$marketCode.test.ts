import { describe, expect, it } from "vitest"
import { Route } from "./$marketCode"

describe("market route shell", () => {
  it("contains no market loader or preload requests", () => {
    expect(Route.options.loader).toBeUndefined()
  })
  it("rejects a static invalid market synchronously", () => {
    const beforeLoad = Route.options.beforeLoad
    if (typeof beforeLoad !== "function") throw new Error("missing guard")
    expect(() =>
      Reflect.apply(beforeLoad, undefined, [
        { params: { marketCode: "unknown" } },
      ])
    ).toThrow(expect.objectContaining({ isNotFound: true }))
  })
})
