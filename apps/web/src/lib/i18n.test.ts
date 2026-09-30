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
    expect(traditionalChinese.t("chatTitle")).toBe("AI智慧問答")
    expect(simplifiedChinese.t("chatTitle")).toBe("AI智能问答")
    expect(english.t("chatTitle")).toBe("AI Q&A")
    expect(traditionalChinese.t("podcastTitle")).toBe("Podcast")
    expect(simplifiedChinese.t("podcastAudioFallback")).toContain("其他可用")
    expect(english.t("podcastAdminTitle")).toBe("Podcast content")
    expect(traditionalChinese.t("reportValueAiServers")).toBe("AI 伺服器")
    expect(simplifiedChinese.t("reportValueForeignTxNetPosition")).toBe(
      "外资 TX 净部位"
    )
    expect(english.t("reportAxisMonday")).toBe("Mon")
    expect(traditionalChinese.t("reportBlockMacroSnapshot")).toBe("商品收盤")
    expect(simplifiedChinese.t("reportBlockMacroSnapshot")).toBe("商品收盘")
    expect(english.t("reportBlockMacroSnapshot")).toBe("Commodity EOD")
    expect(
      traditionalChinese.t("reportBlockMacroCommodityNormalizedPerformance")
    ).toBe("布蘭特原油與黃金標準化表現")
    expect(
      simplifiedChinese.t("reportBlockMacroCommodityNormalizedPerformance")
    ).toBe("布兰特原油与黄金标准化表现")
    expect(english.t("reportBlockMacroCommodityNormalizedPerformance")).toBe(
      "Brent and gold normalized performance"
    )
    expect(traditionalChinese.t("reportMarket_us_equity")).toBe("美國股市")
    expect(traditionalChinese.t("reportMarket_forex")).toBe("外匯市場")
    expect(simplifiedChinese.t("reportMarket_hk_equity")).toBe("香港股市")
    expect(english.t("reportMarket_cn_equity")).toBe("China equities")
    expect(english.t("themeToggleLabel_dark")).toContain("dark")
    expect(english.t("role_org_member")).toBe("Organization member")
    expect(traditionalChinese.language).toBe("zh-hant")
  })
})

describe("news management translations", () => {
  const locales = ["zh-hant", "zh-hans", "en"] as const
  const newsAdminKeys = (locale: (typeof locales)[number]) =>
    Object.keys(
      createI18n(locale).getResourceBundle(locale, "translation") as Record<
        string,
        string
      >
    )
      .filter(key =>
        /^news(Collection|Candidate|Curation|Management)/.test(key)
      )
      .sort()

  it("defines every news management key in all three locales", () => {
    const traditional = newsAdminKeys("zh-hant")
    expect(newsAdminKeys("zh-hans")).toEqual(traditional)
    expect(newsAdminKeys("en")).toEqual(traditional)
  })

  it("covers the overnight collection and headline screen labels", () => {
    const keys = [
      "newsCandidateStage_screened_out",
      "newsCurationFilter_screened_out",
      "newsCurationColScreen",
      "newsCurationScreenLabel",
      "newsCurationScreenSelected",
      "newsCurationPool",
      ...["live", "collected", "both"].map(key => `newsCandidateVia_${key}`),
      "newsCollectionTitle",
      "newsCollectionCurrent",
      "newsCollectionLoading",
      "newsCollectionEmpty",
      ...["never", "cooling", "failing", "ok"].map(
        key => `newsCollectionHealth_${key}`
      ),
      ...["flash", "fast", "normal"].map(
        key => `newsCollectionPollGroup_${key}`
      ),
    ]
    for (const locale of locales) {
      const translation = createI18n(locale).getResourceBundle(
        locale,
        "translation"
      ) as Record<string, string>
      for (const key of keys) {
        expect(translation[key], `${locale}:${key}`).toBeTruthy()
      }
    }
  })
})
