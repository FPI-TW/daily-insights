import { ResponsiveTable } from "./ResponsiveTable"
import type { Locale } from "@daily-insights/api-client"
import { useTranslation } from "react-i18next"
import { directionClass, formatChange, formatNumber } from "#/lib/format"
import { indexNameKey, type MarketIndexHistory } from "#/lib/indices"

const usIndexSymbols = ["^GSPC", "^NDX", "^DJI", "^SOX", "^RUT"] as const

type Performance = {
  close: string | null
  daily: number | null
  monthly: number | null
  ytd: number | null
  symbol: (typeof usIndexSymbols)[number]
}

function percentageChange(close: string, baseline: string | undefined) {
  const latest = Number(close)
  const previous = Number(baseline)
  if (
    !Number.isFinite(latest) ||
    !Number.isFinite(previous) ||
    previous === 0
  ) {
    return null
  }
  return ((latest - previous) / previous) * 100
}

/** Bars are settled closes. Monthly/YTD baselines are the last settled closes
 * strictly before the latest bar's calendar month/year, respectively. */
export function usIndexPerformanceRows(
  history: MarketIndexHistory
): Performance[] {
  const bySymbol = new Map(
    history.series.map(series => [series.symbol, series.bars])
  )
  return usIndexSymbols.map<Performance>(symbol => {
    const bars = [...(bySymbol.get(symbol) ?? [])].sort((a, b) =>
      a.trade_date.localeCompare(b.trade_date)
    )
    const latest = bars.at(-1)
    if (!latest) {
      return { symbol, close: null, daily: null, monthly: null, ytd: null }
    }
    const monthStart = `${latest.trade_date.slice(0, 7)}-01`
    const yearStart = `${latest.trade_date.slice(0, 4)}-01-01`
    const beforeMonth = bars.filter(bar => bar.trade_date < monthStart).at(-1)
    const beforeYear = bars.filter(bar => bar.trade_date < yearStart).at(-1)
    return {
      symbol,
      close: latest.close,
      daily: percentageChange(latest.close, bars.at(-2)?.close),
      monthly: percentageChange(latest.close, beforeMonth?.close),
      ytd: percentageChange(latest.close, beforeYear?.close),
    }
  })
}

export function UsIndexPerformanceTableLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel animate-pulse p-5"
      role="status"
      aria-live="polite"
      aria-label={t("usIndexTableLoading")}
    >
      <div className="h-5 w-48 rounded bg-line" />
      <div className="mt-5 h-52 rounded bg-line" />
    </section>
  )
}

export function UsIndexPerformanceTable({
  history,
  locale,
}: {
  history: MarketIndexHistory | null
  locale: Locale
}) {
  const { t } = useTranslation()
  if (history === null) {
    return (
      <section className="surface-panel p-5" role="status" aria-live="polite">
        <h2 className="m-0 text-base font-extrabold">
          {t("usIndexTableTitle")}
        </h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {t("usIndexTableUnavailable")}
        </p>
      </section>
    )
  }
  const rows = usIndexPerformanceRows(history)
  if (rows.every(row => row.close === null)) {
    return (
      <section className="surface-panel p-5" role="status" aria-live="polite">
        <h2 className="m-0 text-base font-extrabold">
          {t("usIndexTableTitle")}
        </h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {history.failedSymbols.length > 0
            ? t("usIndexTableUnavailable")
            : t("usIndexTableEmpty")}
        </p>
      </section>
    )
  }
  return (
    <section
      className="surface-panel h-full min-w-0 p-5"
      aria-labelledby="us-index-table-title"
    >
      <h2 id="us-index-table-title" className="m-0 text-base font-extrabold">
        {t("usIndexTableTitle")}
      </h2>
      {history.failedSymbols.length > 0 ? (
        <p
          className="mt-3 mb-0 rounded-md border border-market-caution/40 bg-market-caution/10 p-3 text-sm text-sea-ink-soft"
          role="status"
        >
          {t("usIndexTablePartial", {
            symbols: history.failedSymbols.join(", "),
          })}
        </p>
      ) : null}
      <div className="mt-4 min-w-0">
        <ResponsiveTable>
          <thead className="border-y border-line bg-link-hover text-xs font-bold text-sea-ink-soft">
            <tr>
              <th scope="col" className="px-4 py-1.5 text-left @lg:w-[36%]!">
                {t("usIndexTableIndex")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right @lg:w-[20%]">
                {t("usIndexTableClose")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right">
                {t("usIndexTableDaily")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right">
                {t("usIndexTableMonthly")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right">
                {t("usIndexTableYearly")}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map(row => {
              const nameKey = indexNameKey(row.symbol)
              return (
                <tr
                  key={row.symbol}
                  className="border-b border-line last:border-b-0"
                >
                  <th
                    scope="row"
                    className="whitespace-nowrap px-4 py-1 text-left font-semibold text-sea-ink"
                  >
                    <span className="block">{t(nameKey ?? row.symbol)}</span>
                    {nameKey && locale !== "en" ? (
                      <span className="block text-xs leading-4 font-normal text-sea-ink-soft">
                        {t(nameKey, { lng: "en" })}
                      </span>
                    ) : null}
                  </th>
                  <td
                    data-label={t("usIndexTableClose")}
                    className="px-4 py-1 text-right font-mono tabular-nums text-sea-ink"
                  >
                    <span className="whitespace-nowrap">
                      {formatNumber(row.close, "index", locale)}
                    </span>
                  </td>
                  {[row.daily, row.monthly, row.ytd].map((value, column) => {
                    const change = formatChange(value, locale, {
                      flatLabel: t("reportChangeFlat"),
                    })
                    return (
                      <td
                        key={column}
                        data-label={t(
                          [
                            "usIndexTableDaily",
                            "usIndexTableMonthly",
                            "usIndexTableYearly",
                          ][column]!
                        )}
                        className={`whitespace-nowrap px-4 py-1 text-right font-mono tabular-nums ${directionClass(change.direction)}`}
                      >
                        <span className="whitespace-nowrap">{change.text}</span>
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </ResponsiveTable>
      </div>
    </section>
  )
}
