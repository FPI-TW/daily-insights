import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { MarketIndexHistory } from "#/lib/indices"
import {
  UsIndexPerformanceTable,
  UsIndexPerformanceTableLoading,
  usIndexPerformanceRows,
} from "./UsIndexPerformanceTable"

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

describe("usIndexPerformanceRows", () => {
  it("uses the last settled close before calendar month and year boundaries", () => {
    const row = usIndexPerformanceRows(history)[0]
    expect(row).toMatchObject({ symbol: "^GSPC", close: "126" })
    expect(row?.daily).toBeCloseTo(5.0)
    expect(row?.monthly).toBeCloseTo(14.5454545)
    expect(row?.ytd).toBeCloseTo(26)
  })

  it("keeps the prescribed five-index order", () => {
    const rows = usIndexPerformanceRows(history)
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

describe("UsIndexPerformanceTable", () => {
  it("renders five rows without Forward P/E and keeps an accessible partial state", async () => {
    await renderLocalized(
      <UsIndexPerformanceTable
        locale="en"
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
        <UsIndexPerformanceTable locale={locale} history={history} />,
        locale
      )
      expect(screen.getByRole("columnheader", { name: heading })).toBeVisible()
    }
  )

  it("renders loading and unavailable states without failing the report", async () => {
    await renderLocalized(<UsIndexPerformanceTableLoading />)
    expect(screen.getByRole("status")).toHaveAccessibleName()
    cleanup()
    await renderLocalized(
      <UsIndexPerformanceTable locale="en" history={null} />
    )
    expect(screen.getByRole("status")).toHaveTextContent("unavailable")
  })
})
