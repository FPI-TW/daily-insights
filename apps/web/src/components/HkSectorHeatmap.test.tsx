import { cleanup, render, screen, within } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it } from "vitest"
import type {
  SectorHeatmap,
  SectorHeatmapImage,
} from "@daily-insights/api-client"
import { createI18n } from "#/lib/i18n"
import { HkSectorHeatmap, HkSectorHeatmapLoading } from "./HkSectorHeatmap"

function image(
  kind: SectorHeatmapImage["kind"],
  slug: string,
  name: string
): SectorHeatmapImage {
  return {
    kind,
    slug,
    name,
    width: 1920,
    height: kind === "legend" ? 34 : 989,
    url: `https://signed.test/${slug}.png`,
    expires_in_seconds: 900,
  }
}

const whole: SectorHeatmap = {
  data_source: "HSI",
  edition_date: "2026-10-12",
  captured_at: "2026-10-12T00:05:00+00:00",
  images: [
    image("overview", "overview", "Overview"),
    image("legend", "legend", "Legend"),
  ],
}
async function renderLocalized(
  ui: React.ReactNode,
  locale: "zh-hant" | "zh-hans" | "en" = "zh-hant"
) {
  const i18n = createI18n(locale)
  await i18n.changeLanguage(locale)
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}
afterEach(cleanup)

describe("HkSectorHeatmap", () => {
  it("shows the whole overview with the legend and a full-size link", async () => {
    await renderLocalized(<HkSectorHeatmap heatmap={whole} locale="zh-hant" />)
    const region = screen.getByRole("region", { name: "港股產業熱力圖" })
    expect(region).toHaveTextContent("2026年10月12日 收盤")
    const overview = within(region).getByRole("img", {
      name: "恆生指數成分股產業熱力圖總覽",
    })
    expect(overview).toHaveAttribute("src", "https://signed.test/overview.png")
    expect(overview).toHaveAttribute("width", "1920")
    expect(overview).toHaveAttribute("height", "989")
    expect(overview.closest("a")).toHaveAttribute("target", "_blank")
    expect(overview.closest("a")).toHaveAttribute("title", "開啟原始大小")
    expect(
      within(region).getByRole("img", { name: "漲跌幅顏色圖例" })
    ).toHaveAttribute("height", "34")
    expect(within(region).getAllByRole("img")).toHaveLength(2)
  })

  it("announces loading and the empty ledger", async () => {
    await renderLocalized(<HkSectorHeatmapLoading />, "en")
    expect(screen.getByRole("status")).toHaveAccessibleName(
      "Loading the Hong Kong sector heatmap."
    )
    cleanup()
    await renderLocalized(<HkSectorHeatmap heatmap={null} locale="en" />, "en")
    expect(screen.getByRole("status")).toHaveTextContent(
      "No Hong Kong sector heatmap is available yet."
    )
  })
})
