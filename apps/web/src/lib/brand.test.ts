import { describe, expect, it } from "vitest"
import { brandTitleFor, brandTitles, defaultBrandTitle } from "./brand"

describe("localized brand titles", () => {
  it("uses the Chinese title for both Chinese locales", () => {
    expect(brandTitles["zh-hant"]).toBe("廷豐金融晨報智能體")
    expect(brandTitles["zh-hans"]).toBe("廷豐金融晨報智能體")
    expect(brandTitleFor("zh-hant")).toBe("廷豐金融晨報智能體")
    expect(brandTitleFor("zh-hans")).toBe("廷豐金融晨報智能體")
  })

  it("uses the exact English title for the English locale", () => {
    expect(brandTitles.en).toBe("GEAI Daily Insights")
    expect(brandTitleFor("en")).toBe("GEAI Daily Insights")
  })

  it("falls back to the Chinese title when no valid locale is available", () => {
    expect(defaultBrandTitle).toBe("廷豐金融晨報智能體")
    expect(brandTitleFor(undefined)).toBe(defaultBrandTitle)
    expect(brandTitleFor("unsupported")).toBe(defaultBrandTitle)
  })
})
