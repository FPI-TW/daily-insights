import { describe, expect, it } from "vitest"
import { fromTaipeiDateTimeInput, toTaipeiDateTimeInput } from "./trial-members"

describe("trial expiry editing in Taipei time", () => {
  it("crosses the year boundary without using the browser timezone", () => {
    expect(toTaipeiDateTimeInput("2026-12-31T20:30:00Z")).toBe(
      "2027-01-01T04:30"
    )
    expect(fromTaipeiDateTimeInput("2027-01-01T04:30")).toBe(
      "2026-12-31T20:30:00.000Z"
    )
    expect(toTaipeiDateTimeInput(null)).toBe("")
  })
})
