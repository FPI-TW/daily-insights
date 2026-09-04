import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { formatIsoDate, formatNumber } from "#/lib/format"
import { indexNameKey, type MarketIndexHistory } from "#/lib/indices"

export function IndexHistoryLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel mt-6 animate-pulse p-5"
      role="status"
      aria-live="polite"
      aria-label={t("indexChartLoading")}
    >
      <div className="h-5 w-44 rounded bg-line" />
      <div className="mt-5 h-84 rounded-lg bg-line sm:h-96" />
    </section>
  )
}

function symbolLabel(
  symbol: string,
  t: ReturnType<typeof useTranslation>["t"]
) {
  const key = indexNameKey(symbol)
  return key ? `${t(key)} (${symbol})` : symbol
}

export function IndexHistoryChart({
  history,
  locale,
}: {
  history: MarketIndexHistory | null
  locale: Locale
}) {
  const { t } = useTranslation()
  const colors = useChartColors()
  const [selectedSymbol, setSelectedSymbol] = useState(
    history?.series[0]?.symbol ?? ""
  )

  useEffect(() => {
    if (!history?.series.some(item => item.symbol === selectedSymbol)) {
      setSelectedSymbol(history?.series[0]?.symbol ?? "")
    }
  }, [history, selectedSymbol])

  if (history === null) {
    return (
      <section className="surface-panel mt-6 p-5" role="status">
        <h2 className="m-0 text-base font-extrabold">{t("indexChartTitle")}</h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {t("indexChartUnavailable")}
        </p>
      </section>
    )
  }

  const selected =
    history.series.find(item => item.symbol === selectedSymbol) ??
    history.series[0]
  if (!selected) {
    return (
      <section className="surface-panel mt-6 p-5" role="status">
        <h2 className="m-0 text-base font-extrabold">{t("indexChartTitle")}</h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {history.failedSymbols.length > 0
            ? t("indexChartUnavailable")
            : t("indexChartEmpty")}
        </p>
      </section>
    )
  }

  const latest = selected.bars.at(-1)!
  const label = symbolLabel(selected.symbol, t)
  return (
    <section
      className="surface-panel mt-6 min-w-0 p-5"
      aria-labelledby="index-history-title"
    >
      <div className="mb-4 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 id="index-history-title" className="m-0 text-base font-extrabold">
            {t("indexChartTitle")}
          </h2>
          <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
            {t("indexChartRange", {
              start: formatIsoDate(history.start, locale),
              end: formatIsoDate(history.end, locale),
            })}
          </p>
        </div>
        <label className="grid gap-1 text-xs font-bold text-sea-ink-soft">
          {t("indexChartSelect")}
          <select
            className="min-h-10 rounded-md border border-line bg-surface px-3 text-sm text-sea-ink"
            value={selected.symbol}
            onChange={event => setSelectedSymbol(event.target.value)}
          >
            {history.series.map(item => (
              <option key={item.symbol} value={item.symbol}>
                {symbolLabel(item.symbol, t)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {history.failedSymbols.length > 0 ? (
        <p
          className="mb-4 rounded-md border border-market-caution/40 bg-market-caution/10 p-3 text-sm text-sea-ink-soft"
          role="status"
        >
          {t("indexChartPartial", {
            symbols: history.failedSymbols.join(", "),
          })}
        </p>
      ) : null}
      <div className="h-84 min-w-0 w-full overflow-hidden border-y border-line py-2 sm:h-96">
        <ClientOnly
          fallback={
            <div
              className="h-full w-full animate-pulse rounded-lg bg-link-hover"
              role="status"
              aria-label={t("indexChartLoading")}
            />
          }
        >
          <ReactECharts
            style={{ height: "100%", width: "100%" }}
            option={{
              animation: false,
              aria: {
                enabled: true,
                description: `${label}. ${t("indexChartAccessibleSummary")}`,
              },
              color: colors.series,
              grid: { left: 64, right: 18, top: 24, bottom: 72 },
              tooltip: {
                trigger: "axis",
                appendToBody: true,
                valueFormatter: (value: number | string) =>
                  formatNumber(value, null, locale),
              },
              xAxis: {
                type: "category",
                data: selected.bars.map(bar => bar.trade_date),
                boundaryGap: false,
                axisLabel: { color: colors.text },
                axisLine: { lineStyle: { color: colors.grid } },
              },
              yAxis: {
                type: "value",
                scale: true,
                axisLabel: { color: colors.text },
                splitLine: {
                  lineStyle: { color: colors.grid, type: "dashed" },
                },
              },
              dataZoom: [
                { type: "inside", start: 0, end: 100 },
                {
                  type: "slider",
                  height: 18,
                  bottom: 12,
                  borderColor: colors.grid,
                  textStyle: { color: colors.text },
                },
              ],
              series: [
                {
                  name: label,
                  type: "line",
                  data: selected.bars.map(bar => Number(bar.close)),
                  connectNulls: false,
                  showSymbol: false,
                  lineStyle: { width: 2 },
                  areaStyle: { color: "transparent" },
                },
              ],
            }}
          />
        </ClientOnly>
      </div>
      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs text-sea-ink-soft">
        <div className="flex gap-1">
          <dt>{t("indexChartLatestDate")}</dt>
          <dd className="m-0 font-semibold text-sea-ink">
            {formatIsoDate(latest.trade_date, locale)}
          </dd>
        </div>
        <div className="flex gap-1">
          <dt>{t("indexChartLatestClose")}</dt>
          <dd className="m-0 font-mono font-semibold text-sea-ink tabular-nums">
            {formatNumber(latest.close, null, locale)}
          </dd>
        </div>
      </dl>
      <div className="sr-only">
        <table>
          <caption>{t("indexChartAccessibleSummary")}</caption>
          <tbody>
            {selected.bars.map(bar => (
              <tr key={bar.trade_date}>
                <th>{formatIsoDate(bar.trade_date, locale)}</th>
                <td>{formatNumber(bar.close, null, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
