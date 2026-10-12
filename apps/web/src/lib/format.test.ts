import { describe, expect, it } from "vitest"
import {
  directionClass,
  formatChange,
  formatIsoDate,
  formatNumber,
  unitLabel,
} from "./format"

describe("formatNumber", () => {
  it("groups thousands and keeps two decimals for currencies and indexes", () => {
    expect(formatNumber("4437.4020", "usd", "en")).toBe("4,437.40")
    expect(formatNumber("94.3679", "usd", "zh-hant")).toBe("94.37")
    expect(formatNumber("107.3444", "index", "en")).toBe("107.34")
    expect(formatNumber("77590.3600", "usd", "zh-hans")).toBe("77,590.36")
  })

  it("keeps four decimals for small currency amounts", () => {
    expect(formatNumber("0.2046", "usd", "en")).toBe("0.2046")
    expect(formatNumber("-9.99995", "eur", "en")).toBe("-10.0000")
  })

  it("adds the percent sign for percent units", () => {
    expect(formatNumber("98.6301", "percent", "en")).toBe("98.63%")
  })

  it("keeps six decimals for raw commodity ratios", () => {
    expect(formatNumber("0.03", "ratio", "en")).toBe("0.030000")
  })

  it("falls back to two decimals for unknown units and dashes for nulls", () => {
    expect(formatNumber("1234.5678", "widgets", "en")).toBe("1,234.57")
    expect(formatNumber(null, "usd", "en")).toBe("—")
    expect(formatNumber("n/a", "usd", "en")).toBe("n/a")
  })
})

describe("formatChange", () => {
  const flat = { flatLabel: "Flat" }

  it("shows the sign, the percent and the direction", () => {
    expect(formatChange("0.0397", "en", flat)).toEqual({
      text: "+0.04%",
      direction: "up",
    })
    expect(formatChange("-95.1184", "zh-hant", flat)).toEqual({
      text: "-95.12%",
      direction: "down",
    })
    expect(formatChange("1.5", "en", { ...flat, percent: false })).toEqual({
      text: "+1.50",
      direction: "up",
    })
  })

  it("separates zero from missing", () => {
    expect(formatChange("0.0000", "en", flat)).toEqual({
      text: "Flat",
      direction: "flat",
    })
    expect(formatChange(null, "en", flat)).toEqual({
      text: "—",
      direction: "none",
    })
  })

  it("passes pre-formatted text through with its sign direction", () => {
    expect(formatChange("+0.8%", "en", flat)).toEqual({
      text: "+0.8%",
      direction: "up",
    })
  })

  it("maps directions to token classes", () => {
    expect(directionClass("up")).toBe("text-market-up")
    expect(directionClass("down")).toBe("text-market-down")
    expect(directionClass("flat")).toBe("text-sea-ink-soft")
  })
})

describe("unitLabel and dates", () => {
  const t = (key: string) => `[${key}]`

  it("translates known units and upper-cases other currencies", () => {
    expect(unitLabel("index", t)).toBe("[reportUnit_index]")
    expect(unitLabel("usd", t)).toBe("[reportUnit_usd]")
    expect(unitLabel("jpy", t)).toBe("JPY")
    expect(unitLabel("mystery_unit", t)).toBe("mystery_unit")
    expect(unitLabel(null, t)).toBeNull()
  })

  it("formats ISO dates without shifting the day", () => {
    expect(formatIsoDate("2026-09-02", "en")).toBe("Sep 2, 2026")
    expect(formatIsoDate("2026-09-02", "zh-hant")).toBe("2026年9月2日")
  })
})

it("keeps calendar dates stable and formats instants in the explicit product zone", async () => {
  const { formatDateStamp, formatTimestamp } = await import("./format")
  expect(formatDateStamp("2024-02-29")).toBe("2024-02-29")
  expect(formatDateStamp("2026-09-04T20:30:00Z")).toBe("2026-09-05")
  expect(formatDateStamp("2026-09-04T20:30:00Z", "UTC")).toBe("2026-09-04")
  expect(formatTimestamp("2026-09-04T20:30:00Z")).toBe("2026-09-05 04:30")
  expect(formatDateStamp("invalid")).toBe("—")
})
