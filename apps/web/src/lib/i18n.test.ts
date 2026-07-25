import { describe, expect, it } from "vitest"
import { createI18n } from "./i18n"

describe("request-local i18n", () => {
  it("keeps simultaneous locale instances isolated", () => {
    const traditionalChinese = createI18n("zh-hant")
    const simplifiedChinese = createI18n("zh-hans")
    const english = createI18n("en")

    expect(traditionalChinese.t("signIn")).toBe("登入")
    expect(simplifiedChinese.t("signIn")).toBe("登录")
    expect(english.t("signIn")).toBe("Sign in")
    expect(traditionalChinese.t("podcastTitle")).toBe("Podcast")
    expect(simplifiedChinese.t("podcastAudioFallback")).toContain("繁体")
    expect(english.t("podcastAdminTitle")).toBe("Podcast content")
    expect(traditionalChinese.language).toBe("zh-hant")
  })
})
