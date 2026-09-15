import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { formatIsoDate, formatNumber, numberLocales } from "#/lib/format"
import {
  indexNameKey,
  type IndexMovingAverageMap,
  type MarketIndexHistory,
} from "#/lib/indices"

export function IndexHistoryLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="rounded-2xl border border-line bg-surface mt-6 animate-pulse p-5"
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

function indexOptionLabel(
  symbol: string,
  t: ReturnType<typeof useTranslation>["t"]
) {
  const key = indexNameKey(symbol)
  return key ? t(key) : symbol
}

export function IndexHistoryChart({
  history,
  locale,
  movingAverages = null,
}: {
  history: MarketIndexHistory | null
  locale: Locale
  movingAverages?: Promise<IndexMovingAverageMap> | null
}) {
  const { t } = useTranslation()
  const colors = useChartColors()
  const [selectedSymbol, setSelectedSymbol] = useState(
    history?.series[0]?.symbol ?? ""
  )
  const [movingAverageState, setMovingAverageState] = useState<{
    source: Promise<IndexMovingAverageMap> | null
    values: IndexMovingAverageMap
  }>({ source: null, values: {} })

  useEffect(() => {
    let active = true
    const source = movingAverages
    if (!source)
      return () => {
        active = false
      }
    void source.then(values => {
      if (active) setMovingAverageState({ source, values })
    })
    return () => {
      active = false
    }
  }, [movingAverages])

  useEffect(() => {
    if (!history?.series.some(item => item.symbol === selectedSymbol)) {
      setSelectedSymbol(history?.series[0]?.symbol ?? "")
    }
  }, [history, selectedSymbol])

  if (history === null) {
    return (
      <section
        className="rounded-2xl border border-line bg-surface mt-6 p-5"
        role="status"
      >
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
  if (!selected?.bars.length) {
    return (
      <section
        className="rounded-2xl border border-line bg-surface mt-6 p-5"
        role="status"
      >
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
  const selectedMovingAverages =
    movingAverageState.source === movingAverages
      ? movingAverageState.values[selected.symbol]
      : undefined
  const availableMovingAverages = (selectedMovingAverages?.series ?? [])
    .map(series => ({
      period: series.period,
      values: selected.bars.map(
        bar =>
          series.points.find(point => point.trade_date === bar.trade_date)
            ?.value ?? null
      ),
    }))
    .filter(series => series.values.some(value => value !== null))
  const closeLabel = t("indexChartClose")
  const activityLabel = t("volumeLabel")
  const dates = selected.bars.map(bar => bar.trade_date)
  const activityValues = selected.bars.map(bar =>
    bar.volume === null ? null : bar.volume / 100_000_000
  )
  const series = [
    {
      name: closeLabel,
      type: "line" as const,
      data: selected.bars.map(bar => Number(bar.close)),
      connectNulls: false,
      showSymbol: false,
      lineStyle: { width: 3 },
      areaStyle: { color: "transparent" },
    },
    ...availableMovingAverages.map(item => ({
      name: t("indexChartSma", { period: item.period }),
      type: "line" as const,
      data: item.values.map(value => (value === null ? null : Number(value))),
      connectNulls: false,
      showSymbol: false,
      lineStyle: { width: 1.5 },
    })),
  ]
  return (
    <section
      className="rounded-2xl border border-line bg-surface mt-6 min-w-0 p-5"
      aria-labelledby="index-history-title"
    >
      <div className="mb-4 flex flex-wrap items-end justify-between gap-4">
        <h2 id="index-history-title" className="m-0 text-base font-extrabold">
          {t("indexChartTitle")}
        </h2>
        <label className="grid gap-1 text-xs font-bold text-sea-ink-soft">
          {t("indexChartSelect")}
          <select
            className="min-h-10 rounded-md border border-line bg-surface px-3 text-sm text-sea-ink"
            value={selected.symbol}
            onChange={event => setSelectedSymbol(event.target.value)}
          >
            {history.series.map(item => (
              <option key={item.symbol} value={item.symbol}>
                {indexOptionLabel(item.symbol, t)}
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
      <div className="h-108 min-w-0 w-full overflow-hidden border-y border-line py-2 sm:h-120">
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
              color: colors.indexSeries,
              title: [
                {
                  text: activityLabel,
                  left: 4,
                  top: 4,
                  padding: 0,
                  textStyle: {
                    color: colors.text,
                    fontSize: 11,
                    fontWeight: "bold",
                  },
                  coordinateSystem: "matrix",
                  coord: [0, 4],
                },
              ],
              matrix: {
                left: 0,
                right: 0,
                top: 4,
                bottom: 54,
                x: { show: false, data: [null] },
                y: { show: false, data: Array(6).fill(null) },
                body: {
                  data: [
                    {
                      coord: [
                        [0, 0],
                        [0, 3],
                      ],
                      mergeCells: true,
                    },
                    {
                      coord: [
                        [0, 0],
                        [4, 5],
                      ],
                      mergeCells: true,
                    },
                  ],
                },
              },
              grid: [
                {
                  coordinateSystem: "matrix",
                  coord: [0, 0],
                  left: 64,
                  right: 18,
                  top: 42,
                  bottom: 4,
                },
                {
                  coordinateSystem: "matrix",
                  coord: [0, 4],
                  left: 64,
                  right: 18,
                  top: 24,
                  bottom: 2,
                },
              ],
              axisPointer: { link: [{ xAxisIndex: "all" }] },
              tooltip: {
                trigger: "axis",
                appendToBody: true,
                valueFormatter: (value: number | string) =>
                  formatNumber(value, null, locale),
              },
              legend: {
                type: "scroll",
                top: 10,
                right: 18,
                textStyle: { color: colors.text },
              },
              xAxis: [
                {
                  type: "category",
                  data: dates,
                  gridIndex: 0,
                  boundaryGap: false,
                  axisLabel: { show: false },
                  axisTick: { show: false },
                  axisLine: { show: false },
                },
                {
                  type: "category",
                  data: dates,
                  gridIndex: 1,
                  axisLabel: {
                    color: colors.text,
                    hideOverlap: true,
                    formatter: (date: string) => date.slice(0, 7),
                  },
                  axisLine: { lineStyle: { color: colors.grid } },
                },
              ],
              yAxis: [
                {
                  type: "value",
                  gridIndex: 0,
                  scale: true,
                  axisLabel: { color: colors.text },
                  splitLine: {
                    lineStyle: { color: colors.grid, type: "dashed" },
                  },
                },
                {
                  type: "value",
                  gridIndex: 1,
                  min: 0,
                  axisLabel: {
                    color: colors.text,
                    formatter: (value: number) =>
                      new Intl.NumberFormat(numberLocales[locale], {
                        maximumFractionDigits: 2,
                      }).format(value),
                  },
                  splitLine: {
                    lineStyle: { color: colors.grid, type: "dashed" },
                  },
                },
              ],
              dataZoom: [
                {
                  type: "inside",
                  xAxisIndex: [0, 1],
                  start: 0,
                  end: 100,
                },
                {
                  type: "slider",
                  xAxisIndex: [0, 1],
                  height: 18,
                  bottom: 4,
                  borderColor: colors.grid,
                  textStyle: { color: colors.text },
                },
              ],
              series: [
                ...series,
                {
                  name: activityLabel,
                  type: "bar",
                  xAxisIndex: 1,
                  yAxisIndex: 1,
                  data: selected.bars.map((bar, index) => ({
                    value: activityValues[index],
                    itemStyle: {
                      color:
                        bar.open === null
                          ? colors.faint
                          : Number(bar.close) >= Number(bar.open)
                            ? colors.up
                            : colors.down,
                      opacity: 0.55,
                    },
                  })),
                },
              ],
            }}
          />
        </ClientOnly>
      </div>
      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs text-sea-ink-soft">
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
          <thead>
            <tr>
              <th>{t("indexChartDate")}</th>
              <th>{closeLabel}</th>
              <th>{activityLabel}</th>
              {availableMovingAverages.map(item => (
                <th key={item.period}>
                  {t("indexChartSma", { period: item.period })}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {selected.bars.map((bar, index) => (
              <tr key={bar.trade_date}>
                <th>{formatIsoDate(bar.trade_date, locale)}</th>
                <td>{formatNumber(bar.close, null, locale)}</td>
                <td>
                  {bar.volume === null
                    ? ""
                    : new Intl.NumberFormat(numberLocales[locale], {
                        maximumFractionDigits: 2,
                      }).format(bar.volume / 100_000_000)}
                </td>
                {availableMovingAverages.map(item => (
                  <td key={item.period}>
                    {item.values[index] === null
                      ? ""
                      : formatNumber(item.values[index], null, locale)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
