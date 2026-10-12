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
  name: string,
  symbol_count = 0
): SectorHeatmapImage {
  return {
    kind,
    slug,
    name,
    symbol_count,
    width: 1920,
    height: kind === "legend" ? 34 : 953,
    url: `https://signed.test/${slug}.png`,
    expires_in_seconds: 900,
  }
}

const heatmap: SectorHeatmap = {
  data_source: "HSI",
  edition_date: "2026-10-12",
  captured_at: "2026-10-12T00:05:00+00:00",
  images: [
    image("overview", "overview", "Overview"),
    image("legend", "legend", "Legend"),
    image("sector", "finance", "Finance", 21),
    image("sector", "commercial_services", "Commercial Services", 1),
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
  it("shows the overview, legend and every sector with its constituent count", async () => {
    await renderLocalized(
      <HkSectorHeatmap heatmap={heatmap} locale="zh-hant" />
    )
    const region = screen.getByRole("region", { name: "港股產業熱力圖" })
    expect(region).toHaveTextContent("2026年10月12日 收盤")
    const overview = within(region).getByRole("img", {
      name: "恆生指數成分股產業熱力圖總覽",
    })
    expect(overview).toHaveAttribute("src", "https://signed.test/overview.png")
    expect(overview).toHaveAttribute("width", "1920")
    expect(overview).toHaveAttribute("height", "953")
    expect(
      within(region).getByRole("img", { name: "漲跌幅顏色圖例" })
    ).toHaveAttribute("height", "34")
    const finance = within(region).getByRole("img", {
      name: "Finance 產業熱力圖",
    })
    expect(finance).toHaveAttribute("loading", "lazy")
    // The link takes its name from the image inside it; the title is the hint.
    const fullSize = within(region).getAllByRole("link")
    expect(fullSize.map(link => link.getAttribute("href"))).toEqual([
      "https://signed.test/overview.png",
      "https://signed.test/finance.png",
      "https://signed.test/commercial_services.png",
    ])
    expect(fullSize[0]).toHaveAttribute("target", "_blank")
    expect(fullSize[0]).toHaveAttribute("title", "開啟原始大小")
    expect(region).toHaveTextContent("21 檔成分股")
    expect(region).toHaveTextContent("1 檔成分股")
  })

  it("pluralises constituent counts in English", async () => {
    await renderLocalized(
      <HkSectorHeatmap heatmap={heatmap} locale="en" />,
      "en"
    )
    const region = screen.getByRole("region", {
      name: "Hong Kong sector heatmap",
    })
    expect(region).toHaveTextContent("Oct 12, 2026 close")
    expect(region).toHaveTextContent("21 constituents")
    expect(region).toHaveTextContent("1 constituent")
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
