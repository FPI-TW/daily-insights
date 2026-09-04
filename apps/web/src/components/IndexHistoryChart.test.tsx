import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { MarketIndexHistory } from "#/lib/indices"
import { IndexHistoryChart, IndexHistoryLoading } from "./IndexHistoryChart"

vi.mock("@tanstack/react-router", () => ({
  ClientOnly: ({ children }: { children: React.ReactNode }) => children,
}))
vi.mock("echarts-for-react", () => ({
  default: ({ option }: { option: unknown }) => (
    <div data-testid="index-chart">{JSON.stringify(option)}</div>
  ),
}))

const history: MarketIndexHistory = {
  marketCode: "us_equity",
  start: "2024-09-04",
  end: "2026-09-04",
  failedSymbols: ["^SOX"],
  series: [
    {
      symbol: "^DJI",
      bars: [
        {
          symbol: "^DJI",
          market_code: "us_equity",
          trade_date: "2026-09-02",
          open: "45000.0",
          high: "45100.0",
          low: "44900.0",
          close: "45050.5",
          volume: null,
        },
      ],
    },
    {
      symbol: "^GSPC",
      bars: [
        {
          symbol: "^GSPC",
          market_code: "us_equity",
          trade_date: "2026-09-03",
          open: "6500.0",
          high: "6550.0",
          low: "6480.0",
          close: "6525.25",
          volume: null,
        },
      ],
    },
  ],
}

async function renderLocalized(ui: React.ReactNode) {
  const i18n = createI18n("en")
  await i18n.changeLanguage("en")
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}

afterEach(cleanup)

describe("IndexHistoryChart", () => {
  it("switches close series locally and exposes partial failures", async () => {
    await renderLocalized(<IndexHistoryChart history={history} locale="en" />)

    expect(screen.getByTestId("index-chart")).toHaveTextContent("45050.5")
    expect(screen.getByRole("status")).toHaveTextContent("^SOX")

    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "^GSPC" },
    })

    expect(screen.getByTestId("index-chart")).toHaveTextContent("6525.25")
    expect(screen.getByTestId("index-chart")).not.toHaveTextContent("45050.5")
  })

  it("has accessible loading and unavailable states", async () => {
    await renderLocalized(<IndexHistoryLoading />)
    expect(screen.getByRole("status")).toHaveAccessibleName()

    cleanup()
    await renderLocalized(<IndexHistoryChart history={null} locale="en" />)
    expect(screen.getByRole("status")).toHaveTextContent("unavailable")

    cleanup()
    await renderLocalized(
      <IndexHistoryChart
        locale="en"
        history={{ ...history, failedSymbols: [], series: [] }}
      />
    )
    expect(screen.getByRole("status")).toHaveTextContent("no index daily bars")
  })

  it("adds aligned available SMA lines without treating absent averages as a bar failure", async () => {
    await renderLocalized(
      <IndexHistoryChart
        history={{
          ...history,
          failedSymbols: [],
          series: [
            {
              ...history.series[0]!,
              bars: [
                history.series[0]!.bars[0]!,
                {
                  ...history.series[0]!.bars[0]!,
                  trade_date: "2026-09-03",
                  close: "45100.0",
                },
              ],
            },
          ],
        }}
        locale="en"
        movingAverages={Promise.resolve({
          "^DJI": {
            symbol: "^DJI",
            market_code: "us_equity",
            method: "sma",
            price_field: "close",
            formula_version: "sma-close-v1",
            as_of: "2026-09-03",
            series: [
              {
                period: 20,
                points: [
                  { trade_date: "2026-09-02", value: "45000.0" },
                  { trade_date: "2026-09-03", value: "45050.0" },
                ],
              },
              { period: 60, points: [] },
              { period: 120, points: [] },
              { period: 240, points: [] },
            ],
          },
        })}
      />
    )

    await waitFor(() =>
      expect(screen.getByTestId("index-chart")).toHaveTextContent("SMA 20")
    )
    expect(screen.getByTestId("index-chart")).toHaveTextContent("45050")
    expect(screen.getByTestId("index-chart")).not.toHaveTextContent("SMA 60")
    expect(screen.queryByRole("status")).toBeNull()
  })
})
