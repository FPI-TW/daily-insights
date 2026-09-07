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
  MarketViewpoint,
  ReportDetail,
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
  ReportNotGeneratedScreen,
  ReportNotLaunchedScreen,
  ReportOverview,
  ReportShell,
} from "./Reports"
import { LocaleSwitcher } from "./LocaleSwitcher"
import { createI18n } from "#/lib/i18n"
import type { NavMarket } from "#/lib/markets"
import type { ProvisionalReport } from "#/lib/provisional-reports"
import { getProvisionalReport } from "#/test/report-fixtures"

const markets: NavMarket[] = [
  { code: "global_macro_bonds", name: "US Macro and Global Bonds" },
  { code: "crypto", name: "Cryptocurrency" },
  { code: "us_equity", name: "US Equities" },
  { code: "tw_equity", name: "Taiwan Equities" },
]

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
  it("lists every market the API marks visible, in API order", async () => {
    await renderLocalized(
      <ReportShell
        locale="en"
        markets={[...markets, { code: "forex", name: "Foreign Exchange" }]}
      >
        <ReportList />
      </ReportShell>,
      "en"
    )
    const links = screen.getByRole("navigation").querySelectorAll("a")
    expect(Array.from(links).map(link => link.textContent)).toEqual([
      "All markets",
      "Macro analysis",
      "Crypto",
      "US equities",
      "Taiwan equities",
      "Forex",
    ])
    expect(links[2]).toHaveAttribute(
      "data-params",
      JSON.stringify({ locale: "en", marketCode: "crypto" })
    )
  })

  it("shows tab-visible analyst viewpoints in navigation order", async () => {
    await renderLocalized(
      <ReportShell locale="en" markets={markets}>
        <ReportList
          markets={markets.filter(market => market.code !== "crypto")}
          viewpoints={[
            {
              viewpoint_date: "2026-09-02",
              market_code: "global_macro_bonds",
              source_market_code: "us_macro",
              points: ["Bond yields remain range-bound."],
              fetched_at: "2026-09-02T08:00:00+08:00",
            },
            {
              viewpoint_date: "2026-09-02",
              market_code: "crypto",
              source_market_code: "crypto",
              points: ["Crypto is intentionally hidden from the current tabs."],
              fetched_at: "2026-09-02T08:00:00+08:00",
            },
            {
              viewpoint_date: "2026-09-02",
              market_code: "tw_equity",
              source_market_code: "tw_stocks",
              points: ["Taiwan breadth is improving."],
              fetched_at: "2026-09-02T08:00:00+08:00",
            },
          ]}
        />
      </ReportShell>,
      "en"
    )

    const navigation = screen.getByRole("navigation")
    const section = screen.getByRole("region", { name: "Analyst viewpoints" })
    expect(navigation.compareDocumentPosition(section)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING
    )
    expect(section).toHaveTextContent("Global macro")
    expect(section).toHaveTextContent("Taiwan equities")
    expect(section).not.toHaveTextContent("US equities")
    expect(section).not.toHaveTextContent("Crypto")
  })

  it("renders the not-launched state inside the shared shell with a single nav", async () => {
    await renderLocalized(
      <ReportShell locale="en" markets={markets} activeMarket="tw_equity">
        <ReportNotLaunchedScreen locale="en" marketCode="tw_equity" />
      </ReportShell>,
      "en"
    )
    expect(screen.getByRole("status")).toHaveTextContent(
      "Report not launched yet"
    )
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Taiwan equities"
    )
    expect(screen.getAllByRole("navigation")).toHaveLength(1)
  })

  it("formats live metrics with thousands, per-item currencies and signed changes", async () => {
    const report = {
      marketCode: "global_macro_bonds",
      status: "complete",
      editionDate: "2026-09-03",
      sourceDate: "2026-09-02",
      stale: true,
      staleReason: "Provider closed for a holiday.",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_global_macro_bonds",
      blocks: [
        {
          kind: "metric",
          status: "ok",
          titleKey: "reportBlockMacroSnapshot",
          metrics: [
            {
              labelKey: "reportLabelBrent",
              value: { kind: "number", value: "94.3679" },
              change: { kind: "number", value: "0.0397" },
              unitCode: "usd",
            },
            {
              labelKey: "reportLabelGold",
              value: { kind: "number", value: "4437.4020" },
              change: { kind: "number", value: "-1.1312" },
              unitCode: "usd",
            },
            {
              labelKey: "reportLabelCopper",
              value: { kind: "number", value: "24.4000" },
              change: { kind: "number", value: "0.0000" },
              unitCode: "eur",
            },
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(<ReportDetail locale="en" report={report} />, "en")

    expect(screen.getByText("94.37").parentElement).toHaveTextContent(
      "94.37USD+0.04%"
    )
    expect(screen.getByText("+0.04%")).toHaveClass("text-market-up")
    expect(screen.getByText("4,437.40").parentElement).toHaveTextContent(
      "4,437.40USD-1.13%"
    )
    expect(screen.getByText("-1.13%")).toHaveClass("text-market-down")
    expect(screen.getByText("24.40").parentElement).toHaveTextContent(
      "24.40EURFlat"
    )
    expect(screen.getByText("Flat")).toHaveClass("text-sea-ink-soft")
    expect(screen.getByText("Data as of Sep 2, 2026")).toBeVisible()
    expect(screen.getByText("Possibly stale")).toBeVisible()
    expect(screen.getByText("Provider closed for a holiday.")).toBeVisible()
  })

  it("labels table columns with their units and colours percent cells", async () => {
    const report = {
      marketCode: "us_equity",
      status: "complete",
      editionDate: "2026-09-03",
      sourceDate: "2026-09-02",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_us_equity",
      blocks: [
        {
          kind: "table",
          status: "ok",
          titleKey: "reportBlockUsLeaders",
          columns: [
            { labelKey: "reportColumnInstrument", unitCode: null },
            { labelKey: "reportColumnPrice", unitCode: "usd" },
            { labelKey: "reportColumnChange", unitCode: "percent" },
          ],
          rows: [
            [
              { kind: "literal", value: "BURUD" },
              { kind: "number", value: "1.4500" },
              { kind: "number", value: "98.6301" },
            ],
            [
              { kind: "literal", value: "EYES" },
              { kind: "number", value: "77590.3600" },
              { kind: "number", value: "-95.1184" },
            ],
            [{ kind: "literal", value: "ADBT" }, null, null],
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(
      <ReportDetail locale="zh-hant" report={report} />,
      "zh-hant"
    )

    expect(
      screen.getByRole("columnheader", { name: "價格 (USD)" })
    ).toBeVisible()
    expect(screen.getByRole("columnheader", { name: "變動 (%)" })).toBeVisible()
    expect(screen.getByText("1.4500")).toBeVisible()
    expect(screen.getByText("77,590.36")).toBeVisible()
    expect(screen.getByText("+98.63%")).toHaveClass("text-market-up")
    expect(screen.getByText("-95.12%")).toHaveClass("text-market-down")
    expect(screen.getAllByText("—")).toHaveLength(2)
  })

  it("explains missing and errored blocks and surfaces caveats", async () => {
    const report = {
      marketCode: "tw_equity",
      status: "partial",
      editionDate: "2026-09-03",
      sourceDate: null,
      caveat: "TWSE T86 had no data for 2026-09-02.",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_tw_equity",
      blocks: [
        {
          kind: "metric",
          status: "missing",
          titleKey: "reportBlockTaiwanIndex",
          metrics: [],
        },
        {
          kind: "metric",
          status: "error",
          titleKey: "reportBlockBreadth",
          caveat: { kind: "literal", value: "Upstream timeout." },
          metrics: [],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(<ReportDetail locale="en" report={report} />, "en")

    expect(screen.getByText("Some sections missing")).toBeVisible()
    expect(
      screen.getByText("TWSE T86 had no data for 2026-09-02.")
    ).toBeVisible()
    expect(
      screen.getByText("No data was received for this section today.")
    ).toBeVisible()
    expect(
      screen.getByText("This section failed to load its data.")
    ).toBeVisible()
    expect(screen.getByText("Upstream timeout.")).toBeVisible()
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
      expect(screen.getByText("Provider holiday adjustment")).toBeVisible()
    }
  )

  it.each([
    ["zh-hant", "指數"],
    ["zh-hans", "指数"],
    ["en", "Index"],
  ] as const)("translates the chart unit code in %s", async (locale, unit) => {
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
          id: "crypto.overview_series",
          status: "ok",
          titleKey: "reportBlockNormalizedPerformance",
          unitCode: "index",
          series: [
            {
              id: "btc",
              label: { kind: "literal", value: "BTC" },
              points: [{ label: { kind: "literal", value: "D1" }, value: 100 }],
            },
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(
      <ReportDetail locale={locale} report={report} />,
      locale
    )

    expect(screen.getByRole("definition")).toHaveTextContent(unit)
    expect(screen.queryByText("index")).not.toBeInTheDocument()
  })

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

  it("uses separate scaled axes and six-decimal formatting for commodity ratios", async () => {
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
          id: "macro.commodity_ratios",
          status: "ok",
          titleKey: "reportBlockMacroCommodityRatios",
          unitCode: "ratio",
          unitLabel: { kind: "literal", value: "Ratio" },
          series: [
            {
              id: "oil_gold_ratio",
              label: { kind: "literal", value: "Oil-Gold Ratio" },
              points: [
                {
                  label: { kind: "literal", value: "2026-08-29" },
                  value: 0.03,
                },
              ],
            },
            {
              id: "copper_gold_ratio",
              label: { kind: "literal", value: "Copper-Gold Ratio" },
              points: [
                {
                  label: { kind: "literal", value: "2026-08-29" },
                  value: 0.0017,
                },
              ],
            },
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(<ReportDetail locale="en" report={report} />, "en")

    const chart = screen.getByTestId("chart")
    expect(chart).toHaveTextContent('"right":76')
    expect(chart).toHaveTextContent('"yAxisIndex":0')
    expect(chart).toHaveTextContent('"yAxisIndex":1')
    expect(chart).not.toHaveTextContent('"yAxis":100')
    expect(
      screen.getByRole("heading", {
        name: "Oil-Gold / Copper-Gold Ratios",
      })
    ).toBeInTheDocument()
    expect(screen.queryByText("Unit:")).not.toBeInTheDocument()
    expect(screen.getByText("0.030000")).toBeInTheDocument()
  })

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
    ["zh-hant", "8 月 22 日", "上漲", "3,412 億元", "週一"],
    ["zh-hans", "8 月 22 日", "上涨", "3,412 亿元", "周一"],
    ["en", "Aug 22", "Advancers", "NT$341.2 billion", "Mon"],
  ] as const)(
    "localizes provisional table values, units, and chart labels in %s",
    async (locale, axisLabel, breadthLabel, unit, weekdayLabel) => {
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

  it("replaces an unavailable block with a local placeholder", async () => {
    const crypto = await getProvisionalReport("crypto")
    if (!crypto) throw new Error("Expected crypto fixture")

    await renderLocalized(<ReportDetail locale="en" report={crypto} />, "en")

    expect(
      screen.getByText("No data was received for this section today.")
    ).toBeVisible()
    expect(screen.queryByText("Data missing")).not.toBeInTheDocument()
  })

  it("resolves chart colors from tokens and refreshes them after a theme change", async () => {
    const crypto = await getProvisionalReport("crypto")
    if (!crypto) throw new Error("Expected report fixture")

    const root = document.documentElement
    const initialClassName = root.className
    const initialDeep = root.style.getPropertyValue("--chart-1")
    const initialLagoon = root.style.getPropertyValue("--chart-2")
    root.style.setProperty("--chart-1", "rgb(23, 105, 224)")
    root.style.setProperty("--chart-2", "rgb(34, 184, 207)")

    try {
      await renderLocalized(<ReportDetail locale="en" report={crypto} />, "en")
      const chart = screen.getByTestId("chart")
      await waitFor(() =>
        expect(chart).toHaveTextContent('"color":["rgb(23, 105, 224)"')
      )

      root.style.setProperty("--chart-1", "rgb(105, 167, 255)")
      root.style.setProperty("--chart-2", "rgb(34, 211, 238)")
      root.classList.add("dark")
      await waitFor(() =>
        expect(chart).toHaveTextContent('"color":["rgb(105, 167, 255)"')
      )
    } finally {
      root.className = initialClassName
      root.style.setProperty("--chart-1", initialDeep)
      root.style.setProperty("--chart-2", initialLagoon)
    }
  })

  it("uses a local placeholder for unavailable blocks while retaining chart gaps", async () => {
    const complete = await getProvisionalReport("global_macro_bonds")
    const unavailable = await getProvisionalReport("tw_index_derivatives")
    if (!complete || !unavailable) throw new Error("Expected report fixtures")

    await renderLocalized(
      <>
        <ReportDetail locale="en" report={complete} />
        <ReportDetail locale="en" report={unavailable} />
      </>
    )

    expect(
      screen.getAllByText("本區塊今日未取得資料。").length
    ).toBeGreaterThan(0)
    expect(screen.getAllByText("—").length).toBeGreaterThan(0)
    expect(screen.getAllByTestId("chart")[0]).toHaveTextContent(
      '"connectNulls":false'
    )
  })

  it("announces loading and presents a route-error state", async () => {
    invalidate.mockClear()
    await renderLocalized(
      <>
        <ReportLoadingScreen />
        <ReportList locale="en" />
        <ReportErrorScreen error={new Error("fixture failure")} />
      </>
    )

    expect(screen.getByRole("status")).toHaveTextContent(
      "正在載入市場晨間報告。"
    )
    expect(screen.queryByText("目前沒有晨間報告")).not.toBeInTheDocument()
    expect(screen.getByRole("alert")).toHaveTextContent("無法載入晨間報告")
    fireEvent.click(screen.getByRole("button", { name: "重試" }))
    expect(invalidate).toHaveBeenCalledOnce()
  })

  it.each([
    ["zh-hant", "本區塊資料尚未產生。"],
    ["zh-hans", "此区块资料尚未生成。"],
    ["en", "This section has not been generated yet."],
  ] as const)(
    "presents a minimal non-error state in %s when publication is absent",
    async (locale, placeholder) => {
      await renderLocalized(
        <ReportShell locale={locale} markets={markets} activeMarket="us_equity">
          <ReportNotGeneratedScreen locale={locale} marketCode="us_equity" />
        </ReportShell>,
        locale
      )

      expect(screen.getByRole("status")).toHaveTextContent(placeholder)
      expect(screen.queryByRole("alert")).not.toBeInTheDocument()
      expect(screen.getByRole("navigation")).toBeVisible()
    }
  )

  it("summarises each report on the index with headline figures and a link", async () => {
    const macro = {
      marketCode: "global_macro_bonds",
      status: "complete",
      editionDate: "2026-09-04",
      sourceDate: "2026-09-03",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_global_macro_bonds",
      blocks: [
        {
          kind: "metric",
          status: "ok",
          titleKey: "reportBlockMacroSnapshot",
          metrics: [
            {
              labelKey: "reportLabelBrent",
              value: { kind: "number", value: "94.4500" },
              change: { kind: "number", value: "-0.1700" },
              unitCode: "usd",
            },
            {
              labelKey: "reportLabelGold",
              value: { kind: "number", value: "4477.2900" },
              change: { kind: "number", value: "0.0800" },
              unitCode: "usd",
            },
          ],
        },
      ],
    } satisfies ProvisionalReport
    const usEquity = {
      marketCode: "us_equity",
      status: "complete",
      editionDate: "2026-09-04",
      sourceDate: "2026-09-03",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_us_equity",
      blocks: [
        {
          kind: "table",
          status: "ok",
          titleKey: "reportBlockUsMegaCaps",
          columns: [
            { labelKey: "reportColumnInstrument", unitCode: null },
            { labelKey: "reportColumnPrice", unitCode: "usd" },
            { labelKey: "reportColumnChange", unitCode: "percent" },
          ],
          rows: [
            [
              { kind: "literal", value: "TSLA" },
              { kind: "number", value: "376.36" },
              { kind: "number", value: "5.42" },
            ],
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(
      <ReportOverview
        locale="en"
        entries={[
          { summary: macro, detail: macro },
          { summary: usEquity, detail: usEquity },
          { summary: { ...usEquity, marketCode: "crypto" }, detail: null },
        ]}
      />,
      "en"
    )

    const macroCard = screen.getByRole("article", { name: "Global macro" })
    expect(macroCard).toHaveTextContent("Brent crude94.45-0.17%")
    expect(macroCard).toHaveTextContent("Gold4,477.29+0.08%")
    expect(macroCard).toHaveTextContent("Sep 3, 2026")
    expect(
      screen.getByRole("article", { name: "US equities" })
    ).toHaveTextContent("TSLA376.36+5.42%")
    expect(screen.getByRole("article", { name: "Crypto" })).toHaveTextContent(
      "Not generated yet today"
    )
    const links = screen.getAllByRole("link", { name: "Open report" })
    expect(links).toHaveLength(3)
    expect(links[0]).toHaveAttribute(
      "data-params",
      JSON.stringify({ locale: "en", marketCode: "global_macro_bonds" })
    )
  })

  it("opens a market page with the analyst viewpoint and widens a lone block", async () => {
    const report = {
      marketCode: "crypto",
      status: "complete",
      editionDate: "2026-09-04",
      sourceDate: "2026-09-03",
      caveatKey: "reportCaveatLive",
      summaryKey: "reportSummary_crypto",
      blocks: [
        {
          kind: "metric",
          status: "ok",
          titleKey: "reportBlockCryptoSnapshot",
          metrics: [
            {
              labelKey: "reportLabelBitcoin",
              value: { kind: "number", value: "77854.19" },
              change: { kind: "number", value: "0.66" },
              unitCode: "usd",
            },
          ],
        },
      ],
    } satisfies ProvisionalReport

    await renderLocalized(
      <ReportDetail
        locale="en"
        report={report}
        viewpoint={{
          viewpoint_date: "2026-09-04",
          market_code: "crypto",
          source_market_code: "crypto",
          points: ["BTC dominance held near 60%."],
          fetched_at: "2026-09-04T02:22:00+00:00",
        }}
      />,
      "en"
    )

    const viewpoint = screen.getByRole("region", { name: "Analyst viewpoint" })
    expect(viewpoint).toHaveTextContent("BTC dominance held near 60%.")
    const block = screen
      .getByRole("heading", { name: "Crypto market snapshot" })
      .closest("section")
    expect(block).toHaveClass("xl:col-span-2")
    expect(
      viewpoint.compareDocumentPosition(block as Element) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy()

    cleanup()
    await renderLocalized(
      <MarketViewpoint
        viewpoint={{
          viewpoint_date: "2026-09-04",
          market_code: "forex",
          source_market_code: "forex",
          points: ["Dollar softened."],
          fetched_at: "2026-09-04T02:22:00+00:00",
        }}
      />,
      "zh-hant"
    )
    expect(
      screen.getByRole("region", { name: "分析師觀點" })
    ).toHaveTextContent("Dollar softened.")
  })

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
