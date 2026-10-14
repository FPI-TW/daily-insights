import { ResponsiveTable } from "./ResponsiveTable"
import type { Locale } from "@daily-insights/api-client"
import { useTranslation } from "react-i18next"
import { directionClass, formatChange, formatNumber } from "#/lib/format"
import {
  indexNameKey,
  percentageChange,
  type MarketIndexHistory,
} from "#/lib/indices"

type Performance = {
  close: string | null
  daily: number | null
  monthly: number | null
  ytd: number | null
  symbol: string
}

/** Bars are settled closes. Monthly/YTD baselines are the last settled closes
 * strictly before the latest bar's calendar month/year, respectively. */
export function indexPerformanceRows(
  history: MarketIndexHistory,
  symbols: readonly string[]
): Performance[] {
  const bySymbol = new Map(
    history.series.map(series => [series.symbol, series.bars])
  )
  return symbols.map<Performance>(symbol => {
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

export function IndexPerformanceTableLoading({
  titleKey,
}: {
  titleKey: string
}) {
  const { t } = useTranslation()
  const title = t(titleKey)
  return (
    <section
      className="surface-panel animate-pulse p-5"
      role="status"
      aria-live="polite"
      aria-label={t("indexTableLoading", { title })}
    >
      <div className="h-5 w-48 rounded bg-line" />
      <div className="mt-5 h-52 rounded bg-line" />
    </section>
  )
}

/** `symbols` is the row order; a tracked symbol left out (such as a
 * volatility index) is neither listed nor counted as missing. `titleKey`
 * names the translated heading. */
export function IndexPerformanceTable({
  history,
  locale,
  titleKey,
  symbols,
}: {
  history: MarketIndexHistory | null
  locale: Locale
  titleKey: string
  symbols: readonly string[]
}) {
  const { t } = useTranslation()
  const title = t(titleKey)
  if (history === null) {
    return (
      <section className="surface-panel p-5" role="status" aria-live="polite">
        <h2 className="m-0 text-base font-extrabold">{title}</h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {t("indexTableUnavailable", { title })}
        </p>
      </section>
    )
  }
  const rows = indexPerformanceRows(history, symbols)
  const failedSymbols = history.failedSymbols.filter(symbol =>
    symbols.includes(symbol)
  )
  if (rows.every(row => row.close === null)) {
    return (
      <section className="surface-panel p-5" role="status" aria-live="polite">
        <h2 className="m-0 text-base font-extrabold">{title}</h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {failedSymbols.length > 0
            ? t("indexTableUnavailable", { title })
            : t("indexTableEmpty", { title })}
        </p>
      </section>
    )
  }
  return (
    <section
      className="surface-panel min-w-0 flex-1 p-5"
      aria-labelledby="index-table-title"
    >
      <h2 id="index-table-title" className="m-0 text-base font-extrabold">
        {title}
      </h2>
      {failedSymbols.length > 0 ? (
        <p
          className="mt-3 mb-0 rounded-md border border-market-caution/40 bg-market-caution/10 p-3 text-sm text-sea-ink-soft"
          role="status"
        >
          {t("indexTablePartial", {
            symbols: failedSymbols.join(", "),
          })}
        </p>
      ) : null}
      <div className="mt-4 min-w-0">
        <ResponsiveTable>
          <thead className="border-y border-line bg-link-hover text-xs font-bold text-sea-ink-soft">
            <tr>
              <th scope="col" className="px-4 py-1.5 text-left @lg:w-[36%]!">
                {t("indexTableIndex")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right @lg:w-[20%]">
                {t("indexTableClose")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right">
                {t("indexTableDaily")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right">
                {t("indexTableMonthly")}
              </th>
              <th scope="col" className="px-4 py-1.5 text-right">
                {t("indexTableYearly")}
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
                    data-label={t("indexTableClose")}
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
                            "indexTableDaily",
                            "indexTableMonthly",
                            "indexTableYearly",
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
