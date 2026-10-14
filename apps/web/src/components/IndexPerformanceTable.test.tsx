import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { MarketIndexHistory } from "#/lib/indices"
import {
  IndexPerformanceTable,
  IndexPerformanceTableLoading,
  indexPerformanceRows,
} from "./IndexPerformanceTable"

const usSymbols = ["^GSPC", "^NDX", "^DJI", "^SOX", "^RUT"]

const history: MarketIndexHistory = {
  marketCode: "us_equity",
  start: "2024-01-01",
  end: "2026-02-03",
  failedSymbols: [],
  series: [
    {
      symbol: "^GSPC",
      bars: [
        {
          symbol: "^GSPC",
          market_code: "us_equity",
          trade_date: "2025-12-31",
          open: null,
          high: null,
          low: null,
          close: "100",
          volume: null,
          trade_value: null,
        },
        {
          symbol: "^GSPC",
          market_code: "us_equity",
          trade_date: "2026-01-30",
          open: null,
          high: null,
          low: null,
          close: "110",
          volume: null,
          trade_value: null,
        },
        {
          symbol: "^GSPC",
          market_code: "us_equity",
          trade_date: "2026-02-02",
          open: null,
          high: null,
          low: null,
          close: "120",
          volume: null,
          trade_value: null,
        },
        {
          symbol: "^GSPC",
          market_code: "us_equity",
          trade_date: "2026-02-03",
          open: null,
          high: null,
          low: null,
          close: "126",
          volume: null,
          trade_value: null,
        },
      ],
    },
    ...["^NDX", "^DJI", "^SOX", "^RUT"].map(symbol => ({
      symbol,
      bars: [
        {
          symbol,
          market_code: "us_equity" as const,
          trade_date: "2025-12-31",
          open: null,
          high: null,
          low: null,
          close: "100",
          volume: null,
          trade_value: null,
        },
        {
          symbol,
          market_code: "us_equity" as const,
          trade_date: "2026-02-03",
          open: null,
          high: null,
          low: null,
          close: "101",
          volume: null,
          trade_value: null,
        },
      ],
    })),
  ],
}

async function renderLocalized(
  ui: React.ReactNode,
  locale: "zh-hant" | "zh-hans" | "en" = "en"
) {
  const i18n = createI18n(locale)
  await i18n.changeLanguage(locale)
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}

afterEach(cleanup)

describe("indexPerformanceRows", () => {
  it("uses the last settled close before calendar month and year boundaries", () => {
    const row = indexPerformanceRows(history, usSymbols)[0]
    expect(row).toMatchObject({ symbol: "^GSPC", close: "126" })
    expect(row?.daily).toBeCloseTo(5.0)
    expect(row?.monthly).toBeCloseTo(14.5454545)
    expect(row?.ytd).toBeCloseTo(26)
  })

  it("keeps the prescribed five-index order", () => {
    const rows = indexPerformanceRows(history, usSymbols)
    expect(rows.map(row => row.symbol)).toEqual([
      "^GSPC",
      "^NDX",
      "^DJI",
      "^SOX",
      "^RUT",
    ])
    expect(rows[1]).toMatchObject({ daily: 1, monthly: 1, ytd: 1 })
  })
})

describe("IndexPerformanceTable", () => {
  it("renders five rows without Forward P/E and keeps an accessible partial state", async () => {
    await renderLocalized(
      <IndexPerformanceTable
        locale="en"
        titleKey="indexTableTitle_us_equity"
        symbols={usSymbols}
        history={{
          ...history,
          failedSymbols: ["^SOX"],
          series: history.series.filter(series => series.symbol !== "^SOX"),
        }}
      />
    )
    expect(screen.getByRole("table")).toBeInTheDocument()
    expect(screen.getAllByRole("row")).toHaveLength(6)
    expect(
      screen.getByRole("columnheader", { name: "Year change" })
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("columnheader", { name: "Forward P/E" })
    ).not.toBeInTheDocument()
    expect(screen.getAllByText("—")).toHaveLength(4)
    expect(screen.getByRole("status")).toHaveTextContent("^SOX")
  })

  it.each([
    ["zh-hant", "年漲跌"],
    ["zh-hans", "年涨跌"],
    ["en", "Year change"],
  ] as const)(
    "localizes the yearly-change heading for %s",
    async (locale, heading) => {
      await renderLocalized(
        <IndexPerformanceTable
          locale={locale}
          titleKey="indexTableTitle_us_equity"
          symbols={usSymbols}
          history={history}
        />,
        locale
      )
      expect(screen.getByRole("columnheader", { name: heading })).toBeVisible()
    }
  )

  it("adds the English index name under a localized name only", async () => {
    await renderLocalized(
      <IndexPerformanceTable
        locale="zh-hant"
        titleKey="indexTableTitle_us_equity"
        symbols={usSymbols}
        history={history}
      />,
      "zh-hant"
    )
    const row = screen.getByRole("rowheader", { name: /標普 500 指數/ })
    expect(row).toHaveTextContent("S&P 500 Index")
    cleanup()
    await renderLocalized(
      <IndexPerformanceTable
        locale="en"
        titleKey="indexTableTitle_us_equity"
        symbols={usSymbols}
        history={history}
      />,
      "en"
    )
    expect(
      screen.getByRole("rowheader", { name: "S&P 500 Index" })
    ).toBeInTheDocument()
  })

  it("renders loading and unavailable states without failing the report", async () => {
    await renderLocalized(
      <IndexPerformanceTableLoading titleKey="indexTableTitle_us_equity" />
    )
    expect(screen.getByRole("status")).toHaveAccessibleName(
      "Loading US five-index performance."
    )
    cleanup()
    await renderLocalized(
      <IndexPerformanceTable
        locale="en"
        titleKey="indexTableTitle_us_equity"
        symbols={usSymbols}
        history={null}
      />
    )
    expect(screen.getByRole("status")).toHaveTextContent(
      "US five-index performance is temporarily unavailable."
    )
  })

  it("lists only the Hong Kong symbols the page asks for, so the volatility index is neither a row nor a gap", async () => {
    const bar = (symbol: string, trade_date: string, close: string) => ({
      symbol,
      market_code: "hk_equity" as const,
      trade_date,
      open: null,
      high: null,
      low: null,
      close,
      volume: null,
      trade_value: null,
    })
    await renderLocalized(
      <IndexPerformanceTable
        locale="en"
        titleKey="indexTableTitle_hk_equity"
        symbols={["^HSI", "^HSCE"]}
        history={{
          marketCode: "hk_equity",
          start: "2024-10-09",
          end: "2026-10-12",
          failedSymbols: ["^HSIL"],
          series: [
            {
              symbol: "^HSI",
              bars: [
                bar("^HSI", "2025-12-31", "20000"),
                bar("^HSI", "2026-10-09", "24000"),
                bar("^HSI", "2026-10-12", "24240"),
              ],
            },
            { symbol: "^HSCE", bars: [bar("^HSCE", "2026-10-12", "8500")] },
            { symbol: "^HSIL", bars: [bar("^HSIL", "2026-10-12", "22.5")] },
          ],
        }}
      />
    )
    expect(
      screen.getByRole("region", { name: "Hong Kong index performance" })
    ).toBeVisible()
    expect(
      screen.getAllByRole("rowheader").map(cell => cell.textContent)
    ).toEqual(["Hang Seng Index", "Hang Seng China Enterprises Index"])
    expect(screen.queryByText("22.50")).not.toBeInTheDocument()
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
    const hsi = screen.getByRole("rowheader", {
      name: "Hang Seng Index",
    }).parentElement!
    expect(hsi).toHaveTextContent("24,240.00")
    expect(hsi).toHaveTextContent("+1.00%")
    expect(hsi).toHaveTextContent("+21.20%")
  })
})
