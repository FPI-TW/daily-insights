import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { describe, expect, it, vi } from "vitest"
import {
  ReportDetail,
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
  ReportNotGeneratedScreen,
} from "./Reports"
import { LocaleSwitcher } from "./LocaleSwitcher"
import { createI18n } from "#/lib/i18n"
import {
  getProvisionalReport,
  getProvisionalReportList,
  type ProvisionalReport,
} from "#/lib/provisional-reports"

const invalidate = vi.fn()
let renderClientOnlyFallback = false

vi.mock("@tanstack/react-router", () => ({
  ClientOnly: ({
    children,
    fallback,
  }: {
    children: React.ReactNode
    fallback: React.ReactNode
  }) => (renderClientOnlyFallback ? fallback : children),
  Link: ({
    children,
    params,
    to,
  }: {
    children: React.ReactNode
    params: unknown
    to: string
  }) => (
    <a data-params={JSON.stringify(params)} data-to={to} href="#reports">
      {children}
    </a>
  ),
  useRouter: () => ({ invalidate }),
}))
vi.mock("echarts-for-react", () => ({
  default: ({
    option,
  }: {
    option: {
      series: Array<{ data: Array<number | null> }>
      xAxis: { data: string[] }
    }
  }) => <div data-testid="chart">{JSON.stringify(option)}</div>,
}))

async function renderLocalized(
  ui: React.ReactNode,
  locale: "zh-hant" | "zh-hans" | "en" = "zh-hant"
) {
  cleanup()
  const i18n = createI18n(locale)
  await i18n.changeLanguage(locale)
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}

describe("three-market report presentation", () => {
  it("shows only three formal market tabs and labels Taiwan direct routes as previews", async () => {
    const reports = await getProvisionalReportList()
    await renderLocalized(<ReportList locale="en" reports={reports} />, "en")
    expect(screen.getByRole("navigation").querySelectorAll("a")).toHaveLength(4)
    expect(screen.queryByText("Taiwan equities")).not.toBeInTheDocument()

    const taiwan = await getProvisionalReport("tw_equity")
    if (!taiwan) throw new Error("Expected Taiwan preview fixture")
    await renderLocalized(<ReportDetail locale="en" report={taiwan} />, "en")
    expect(screen.getByText(/Not launched \/ illustrative data/)).toBeVisible()
  })
  it("uses an accessible in-frame fallback before chart hydration", async () => {
    const crypto = await getProvisionalReport("crypto")
    if (!crypto) throw new Error("Expected report fixture")

    renderClientOnlyFallback = true
    try {
      await renderLocalized(<ReportDetail locale="en" report={crypto} />, "en")

      const fallback = screen.getByRole("status", {
        name: "Textual chart data summary",
      })
      expect(fallback).toHaveClass("h-full", "w-full")
      expect(screen.queryByTestId("chart")).not.toBeInTheDocument()
    } finally {
      renderClientOnlyFallback = false
    }
  })

  it("renders every normalized crypto series without dropping gaps", async () => {
    const report = {
      marketCode: "crypto",
      status: "complete",
      editionDate: "2026-08-30",
      sourceDate: "2026-08-29",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_crypto",
      blocks: [
        {
          kind: "series",
          status: "ok",
          titleKey: "reportBlockNormalizedPerformance",
          series: [
            {
              id: "btc",
              label: { kind: "literal", value: "BTC" },
              points: [
                { label: { kind: "literal", value: "D1" }, value: 100 },
                { label: { kind: "literal", value: "D2" }, value: null },
              ],
            },
            {
              id: "eth",
              label: { kind: "literal", value: "ETH" },
              points: [
                { label: { kind: "literal", value: "D1" }, value: 100 },
                { label: { kind: "literal", value: "D2" }, value: 104 },
              ],
            },
          ],
        },
      ],
    } satisfies ProvisionalReport
    await renderLocalized(<ReportDetail locale="en" report={report} />, "en")
    const chart = screen.getByTestId("chart")
    expect(chart).toHaveTextContent('"name":"BTC"')
    expect(chart).toHaveTextContent('"name":"ETH"')
    expect(chart).toHaveTextContent("null")
  })

  it.each([
    ["zh-hant", "加密資產標準化表現", "比特幣", "乙太幣", "基期 100"],
    ["zh-hans", "加密资产标准化表现", "比特币", "以太币", "基期 100"],
    ["en", "Normalized crypto performance", "Bitcoin", "Ether", "Base 100"],
  ] as const)(
    "uses localized live series labels and preserves chart semantics in %s",
    async (locale, title, bitcoin, ether, base100) => {
      const report = {
        marketCode: "crypto",
        status: "complete",
        editionDate: "2026-08-30",
        sourceDate: "2026-08-29",
        caveatKey: "reportCaveatLive",
        summaryKey: "reportSummary_crypto",
        blocks: [
          {
            kind: "series",
            id: "crypto.normalized_performance",
            status: "ok",
            titleKey: "reportBlockNormalizedPerformance",
            title: { kind: "literal", value: title },
            unitCode: "index",
            unitLabel: { kind: "literal", value: base100 },
            sourceDate: "2026-08-29",
            caveat: { kind: "literal", value: "Provider holiday adjustment" },
            series: [
              {
                id: "btc",
                label: { kind: "literal", value: bitcoin },
                points: [
                  {
                    label: { kind: "literal", value: "2026-08-28" },
                    value: 100,
                  },
                ],
              },
              {
                id: "eth",
                label: { kind: "literal", value: ether },
                points: [
                  {
                    label: { kind: "literal", value: "2026-08-29" },
                    value: 104,
                  },
                ],
              },
            ],
          },
        ],
      } satisfies ProvisionalReport

      await renderLocalized(
        <ReportDetail locale={locale} report={report} />,
        locale
      )

      const chart = screen.getByTestId("chart")
      expect(screen.getByRole("heading", { name: title })).toBeVisible()
      expect(chart).toHaveTextContent(`"name":"${bitcoin}"`)
      expect(chart).toHaveTextContent(`"name":"${ether}"`)
      expect(chart).toHaveTextContent('"data":["2026-08-28","2026-08-29"]')
      expect(chart).toHaveTextContent('"data":[100,null]')
      expect(chart).toHaveTextContent('"data":[null,104]')
      expect(chart).toHaveTextContent('"type":"scroll"')
      expect(chart).toHaveTextContent('"yAxis":100')
      expect(chart).toHaveTextContent(`"formatter":"${base100}"`)
      expect(screen.getAllByText("2026-08-29")).toHaveLength(2)
      expect(screen.getByText("Provider holiday adjustment")).toBeVisible()
    }
  )

  it.each([
    ["zh-hant", "布蘭特原油與黃金標準化表現", "指數（基期 100）", "基期 100"],
    ["zh-hans", "布兰特原油与黄金标准化表现", "指数（基期 100）", "基期 100"],
    [
      "en",
      "Brent and gold normalized performance",
      "Index (Base 100)",
      "Base 100",
    ],
  ] as const)(
    "uses Base-100 chart semantics for the formal macro normalized block in %s",
    async (locale, title, axisName, base100) => {
      const report = {
        marketCode: "global_macro_bonds",
        status: "complete",
        editionDate: "2026-08-30",
        sourceDate: "2026-08-29",
        caveatKey: "reportCaveatLive",
        summaryKey: "reportSummary_global_macro_bonds",
        blocks: [
          {
            kind: "series",
            id: "macro.commodity_normalized_performance",
            status: "ok",
            titleKey: "reportBlockMacroCommodityNormalizedPerformance",
            unitCode: "index",
            series: [
              {
                id: "brent",
                label: { kind: "literal", value: "BRENT" },
                points: [
                  { label: { kind: "literal", value: "D1" }, value: 100 },
                ],
              },
              {
                id: "gold",
                label: { kind: "literal", value: "GOLD" },
                points: [
                  { label: { kind: "literal", value: "D1" }, value: 100 },
                ],
              },
            ],
          },
        ],
      } satisfies ProvisionalReport

      await renderLocalized(
        <ReportDetail locale={locale} report={report} />,
        locale
      )
      expect(screen.getByRole("heading", { name: title })).toBeVisible()
      const chart = screen.getByTestId("chart")
      expect(chart).toHaveTextContent('"name":"BRENT"')
      expect(chart).toHaveTextContent('"name":"GOLD"')
      expect(chart).toHaveTextContent(`"name":"${axisName}"`)
      expect(chart).toHaveTextContent(`"formatter":"${base100}"`)
      expect(chart).toHaveTextContent('"yAxis":100')
    }
  )

  it("sorts an uneven ISO-date category union and aligns each series with null gaps", async () => {
    const report = {
      marketCode: "crypto",
      status: "complete",
      editionDate: "2026-08-30",
      sourceDate: "2026-08-30",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_crypto",
      blocks: [
        {
          kind: "series",
          status: "ok",
          titleKey: "reportBlockNormalizedPerformance",
          series: [
            {
              id: "first",
              label: { kind: "literal", value: "First" },
              points: [
                { label: { kind: "literal", value: "2026-08-28" }, value: 100 },
                { label: { kind: "literal", value: "2026-08-30" }, value: 103 },
              ],
            },
            {
              id: "second",
              label: { kind: "literal", value: "Second" },
              points: [
                { label: { kind: "literal", value: "2026-08-27" }, value: 98 },
                {
                  label: { kind: "literal", value: "2026-08-29" },
                  value: null,
                },
                { label: { kind: "literal", value: "2026-08-30" }, value: 104 },
              ],
            },
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(<ReportDetail locale="en" report={report} />, "en")

    const chart = screen.getByTestId("chart")
    expect(chart).toHaveTextContent(
      '"data":["2026-08-27","2026-08-28","2026-08-29","2026-08-30"]'
    )
    expect(chart).toHaveTextContent('"data":[null,100,null,103]')
    expect(chart).toHaveTextContent('"data":[98,null,null,104]')
  })

  it.each([
    ["zh-hant", "8 月 22 日", "上漲", "3,412 億元", "週一", "穩定幣供給"],
    ["zh-hans", "8 月 22 日", "上涨", "3,412 亿元", "周一", "稳定币供给"],
    [
      "en",
      "Aug 22",
      "Advancers",
      "NT$341.2 billion",
      "Mon",
      "Stablecoin supply",
    ],
  ] as const)(
    "localizes provisional table values, units, and chart labels in %s",
    async (
      locale,
      axisLabel,
      breadthLabel,
      unit,
      weekdayLabel,
      stablecoinLabel
    ) => {
      const crypto = await getProvisionalReport("crypto")
      const taiwan = await getProvisionalReport("tw_equity")
      const usEquity = await getProvisionalReport("us_equity")
      if (!crypto || !taiwan || !usEquity) {
        throw new Error("Expected report fixtures")
      }

      await renderLocalized(
        <>
          <ReportDetail locale={locale} report={crypto} />
          <ReportDetail locale={locale} report={taiwan} />
          <ReportDetail locale={locale} report={usEquity} />
        </>,
        locale
      )

      expect(screen.getByText(axisLabel)).toBeVisible()
      expect(screen.getByText(breadthLabel)).toBeVisible()
      expect(screen.getByText(unit)).toBeVisible()
      expect(screen.getByText(weekdayLabel)).toBeVisible()
      expect(screen.getByText(stablecoinLabel)).toBeVisible()
    }
  )

  it("renders an explicit null metric change while omitting an absent change", async () => {
    const report = {
      marketCode: "tw_equity",
      status: "complete",
      editionDate: "2026-08-28",
      sourceDate: "2026-08-28",
      caveatKey: "reportCaveatMock",
      summaryKey: "reportSummary_tw_equity",
      blocks: [
        {
          kind: "metric",
          status: "ok",
          titleKey: "reportBlockTaiwanIndex",
          metrics: [
            {
              labelKey: "reportLabelTaiex",
              value: { kind: "literal", value: "22,184" },
              change: null,
            },
            {
              labelKey: "reportLabelTurnover",
              value: { kind: "literal", value: "341.2" },
            },
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(<ReportDetail locale="en" report={report} />, "en")

    expect(screen.getByText("TAIEX").parentElement).toHaveTextContent(
      "TAIEX22,184—"
    )
    expect(screen.getByText("Turnover").parentElement).toHaveTextContent(
      "Turnover341.2"
    )
  })

  it("resolves chart colors from tokens and refreshes them after a theme change", async () => {
    const crypto = await getProvisionalReport("crypto")
    if (!crypto) throw new Error("Expected report fixture")

    const root = document.documentElement
    const initialClassName = root.className
    const initialDeep = root.style.getPropertyValue("--lagoon-deep")
    const initialLagoon = root.style.getPropertyValue("--lagoon")
    root.style.setProperty("--lagoon-deep", "rgb(23, 105, 224)")
    root.style.setProperty("--lagoon", "rgb(34, 184, 207)")

    try {
      await renderLocalized(<ReportDetail locale="en" report={crypto} />, "en")
      const chart = screen.getByTestId("chart")
      await waitFor(() =>
        expect(chart).toHaveTextContent('"color":["rgb(23, 105, 224)"')
      )

      root.style.setProperty("--lagoon-deep", "rgb(105, 167, 255)")
      root.style.setProperty("--lagoon", "rgb(34, 211, 238)")
      root.classList.add("dark")
      await waitFor(() =>
        expect(chart).toHaveTextContent('"color":["rgb(105, 167, 255)"')
      )
    } finally {
      root.className = initialClassName
      root.style.setProperty("--lagoon-deep", initialDeep)
      root.style.setProperty("--lagoon", initialLagoon)
    }
  })

  it("visibly represents translated ok, missing, and error block states while retaining chart gaps", async () => {
    const complete = await getProvisionalReport("global_macro_bonds")
    const unavailable = await getProvisionalReport("tw_index_derivatives")
    if (!complete || !unavailable) throw new Error("Expected report fixtures")

    await renderLocalized(
      <>
        <ReportDetail locale="en" report={complete} />
        <ReportDetail locale="en" report={unavailable} />
      </>
    )

    expect(screen.getAllByText("資料完整").length).toBeGreaterThan(0)
    expect(screen.getAllByText("資料缺漏").length).toBeGreaterThan(0)
    expect(screen.getByText("資料錯誤")).toBeVisible()
    expect(screen.getAllByText("—").length).toBeGreaterThan(0)
    expect(screen.getAllByTestId("chart")[0]).toHaveTextContent(
      '"connectNulls":false'
    )
    expect(screen.getAllByTestId("chart")[1]).toHaveTextContent("null")
  })

  it("announces loading and presents localized empty and route-error states", async () => {
    invalidate.mockClear()
    await renderLocalized(
      <>
        <ReportLoadingScreen />
        <ReportList locale="en" reports={[]} />
        <ReportErrorScreen error={new Error("fixture failure")} />
      </>
    )

    expect(screen.getByRole("status")).toHaveTextContent(
      "正在載入市場晨間報告。"
    )
    expect(screen.getByText("目前沒有晨間報告")).toBeVisible()
    expect(screen.getByRole("alert")).toHaveTextContent("無法載入晨間報告")
    fireEvent.click(screen.getByRole("button", { name: "重試" }))
    expect(invalidate).toHaveBeenCalledOnce()
  })

  it.each([
    ["zh-hant", "晨報尚未產生"],
    ["zh-hans", "晨报尚未生成"],
    ["en", "Morning report not generated yet"],
  ] as const)(
    "presents a dedicated non-error state in %s when publication is absent",
    async (locale, title) => {
      await renderLocalized(
        <ReportNotGeneratedScreen locale={locale} marketCode="us_equity" />,
        locale
      )

      expect(screen.getByRole("status")).toHaveTextContent(title)
      expect(screen.queryByRole("alert")).not.toBeInTheDocument()
      expect(screen.getByRole("navigation")).toBeVisible()
    }
  )

  it("keeps the current market code in typed locale-switcher links", async () => {
    await renderLocalized(
      <LocaleSwitcher
        destination="customer-reports"
        locale="zh-hant"
        reportMarketCode="crypto"
      />
    )

    const simplifiedChinese = screen.getByRole("link", { name: "简中" })
    expect(simplifiedChinese).toHaveAttribute(
      "data-to",
      "/$locale/reports/$marketCode"
    )
    expect(simplifiedChinese).toHaveAttribute(
      "data-params",
      JSON.stringify({ locale: "zh-hans", marketCode: "crypto" })
    )
  })
})
