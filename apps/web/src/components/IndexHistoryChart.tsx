import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { z } from "zod"
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

const zoomRangeSchema = z.object({
  start: z.number().min(0).max(100),
  end: z.number().min(0).max(100),
})
const zoomEventSchema = z.union([
  zoomRangeSchema,
  z.object({ batch: z.array(zoomRangeSchema).min(1) }),
])
const tooltipSchema = z.array(
  z.object({
    axisValue: z.union([z.string(), z.number()]),
    seriesName: z.string(),
    dataIndex: z.number(),
  })
)

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
  const [zoom, setZoom] = useState({ start: 0, end: 100 })
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
  const previous = selected.bars.at(-2)
  const change = previous
    ? (Number(latest.close) / Number(previous.close) - 1) * 100
    : null
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
  const rsiByDate = new Map(
    (selectedMovingAverages?.rsi.points ?? []).map(point => [
      point.trade_date,
      point.value,
    ])
  )
  const rsiValues = dates.map(date => {
    const value = rsiByDate.get(date)
    return value == null ? null : Number(value)
  })
  const macdByDate = new Map(
    (selectedMovingAverages?.macd.points ?? []).map(point => [
      point.trade_date,
      point,
    ])
  )
  const macdValues = dates.map(date => {
    const value = macdByDate.get(date)?.macd
    return value == null ? null : Number(value)
  })
  const signalValues = dates.map(date => {
    const value = macdByDate.get(date)?.signal
    return value == null ? null : Number(value)
  })
  const histogramValues = dates.map(date => {
    const value = macdByDate.get(date)?.histogram
    return value == null ? null : Number(value)
  })
  const macdLabel = t("indexChartMacd")
  const signalLabel = t("indexChartMacdSignal")
  const histogramLabel = t("indexChartMacdHistogram")
  const rsiLabel = t("indexChartRsi")
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
  const legendSelection = Object.fromEntries(
    availableMovingAverages.map(item => [
      t("indexChartSma", { period: item.period }),
      item.period !== 240,
    ])
  )
  return (
    <section
      className="rounded-2xl border border-line bg-surface mt-6 min-w-0 p-5"
      aria-labelledby="index-history-title"
    >
      <div className="mb-4">
        <h2 id="index-history-title" className="m-0 text-base font-extrabold">
          {t("indexChartTitle")}
        </h2>
        <div className="mt-3 flex flex-wrap items-end justify-between gap-3">
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
          <div
            className="flex flex-wrap items-baseline justify-end gap-x-3 gap-y-1 text-xs text-sea-ink-soft"
            data-testid="index-latest-summary"
          >
            <span>{closeLabel}</span>
            <strong className="font-mono text-[17px] text-sea-ink tabular-nums">
              {formatNumber(latest.close, null, locale)}
            </strong>
            <span
              className={`inline-flex items-baseline gap-2 whitespace-nowrap font-mono font-bold ${change === null || Math.abs(change) < 0.005 ? "text-sea-ink-soft" : change > 0 ? "text-market-up" : "text-market-down"}`}
            >
              {change !== null && Math.abs(change) >= 0.005 ? (
                <span>{change > 0 ? "▲" : "▼"}</span>
              ) : null}
              <span>
                {change === null
                  ? "—"
                  : `${new Intl.NumberFormat(numberLocales[locale], {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                      signDisplay: "never",
                    }).format(
                      Math.abs(change) < 0.005 ? 0 : Math.abs(change)
                    )}%`}
              </span>
            </span>
            <span>{activityLabel}</span>
            <strong className="font-mono text-[13px] text-sea-ink tabular-nums">
              {formatNumber(activityValues.at(-1), null, locale)}
            </strong>
          </div>
        </div>
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
      <div className="h-168 min-w-0 w-full overflow-hidden border-y border-line py-2 sm:h-184">
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
            notMerge
            style={{ height: "100%", width: "100%" }}
            onEvents={{
              datazoom: (event: unknown) => {
                const parsed = zoomEventSchema.safeParse(event)
                if (!parsed.success) return
                const range =
                  "batch" in parsed.data ? parsed.data.batch[0]! : parsed.data
                if (range.start <= range.end) setZoom(range)
              },
            }}
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
                {
                  text: macdLabel,
                  left: 4,
                  top: 4,
                  padding: 0,
                  textStyle: {
                    color: colors.text,
                    fontSize: 11,
                    fontWeight: "bold",
                  },
                  coordinateSystem: "matrix",
                  coord: [0, 6],
                },
                {
                  text: rsiLabel,
                  left: 4,
                  top: 4,
                  padding: 0,
                  textStyle: {
                    color: colors.text,
                    fontSize: 11,
                    fontWeight: "bold",
                  },
                  coordinateSystem: "matrix",
                  coord: [0, 9],
                },
              ],
              matrix: {
                left: 0,
                right: 0,
                top: 4,
                bottom: 54,
                x: { show: false, data: [null] },
                y: { show: false, data: Array(12).fill(null) },
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
                    {
                      coord: [
                        [0, 0],
                        [6, 8],
                      ],
                      mergeCells: true,
                    },
                    {
                      coord: [
                        [0, 0],
                        [9, 11],
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
                {
                  coordinateSystem: "matrix",
                  coord: [0, 6],
                  left: 64,
                  right: 18,
                  top: 24,
                  bottom: 2,
                },
                {
                  coordinateSystem: "matrix",
                  coord: [0, 9],
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
                renderMode: "richText",
                axisPointer: { type: "cross" },
                formatter: (input: unknown) => {
                  const parsed = tooltipSchema.safeParse(input)
                  if (!parsed.success || !parsed.data[0]) return ""
                  const index = parsed.data[0].dataIndex
                  const bar = selected.bars[index]
                  if (!bar) return ""
                  return [
                    bar.trade_date,
                    `${closeLabel}: ${formatNumber(bar.close, null, locale)}`,
                    `${activityLabel}: ${formatNumber(activityValues[index], null, locale)}`,
                    `${macdLabel}: ${formatNumber(macdValues[index], null, locale)}`,
                    `${signalLabel}: ${formatNumber(signalValues[index], null, locale)}`,
                    `${histogramLabel}: ${formatNumber(histogramValues[index], null, locale)}`,
                    `${rsiLabel}: ${formatNumber(rsiValues[index], null, locale)}`,
                    ...availableMovingAverages.map(
                      item =>
                        `${t("indexChartSma", { period: item.period })}: ${formatNumber(item.values[index], null, locale)}`
                    ),
                  ].join("\n")
                },
              },
              legend: {
                type: "scroll",
                top: 10,
                right: 18,
                data: [
                  closeLabel,
                  ...availableMovingAverages.map(item =>
                    t("indexChartSma", { period: item.period })
                  ),
                ],
                selected: legendSelection,
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
                  axisLabel: { show: false },
                  axisTick: { show: false },
                  axisLine: { show: false },
                },
                {
                  type: "category",
                  data: dates,
                  gridIndex: 2,
                  axisLabel: { show: false },
                  axisTick: { show: false },
                  axisLine: { show: false },
                },
                {
                  type: "category",
                  data: dates,
                  gridIndex: 3,
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
                {
                  type: "value",
                  gridIndex: 2,
                  scale: true,
                  splitNumber: 3,
                  axisLabel: {
                    color: colors.text,
                    hideOverlap: true,
                    formatter: (value: number) =>
                      new Intl.NumberFormat(numberLocales[locale], {
                        maximumFractionDigits: 2,
                      }).format(value),
                  },
                  splitLine: {
                    lineStyle: { color: colors.grid, type: "dashed" },
                  },
                },
                {
                  type: "value",
                  gridIndex: 3,
                  min: 0,
                  max: 100,
                  interval: 30,
                  axisLabel: { color: colors.text },
                  splitLine: {
                    lineStyle: { color: colors.grid, type: "dashed" },
                  },
                },
              ],
              dataZoom: [
                {
                  type: "inside",
                  xAxisIndex: [0, 1, 2, 3],
                  ...zoom,
                },
                {
                  type: "slider",
                  xAxisIndex: [0, 1, 2, 3],
                  ...zoom,
                  height: 26,
                  bottom: 4,
                  borderColor: colors.grid,
                  backgroundColor: colors.surface,
                  fillerColor: colors.gridSoft,
                  showDataShadow: false,
                  brushSelect: false,
                  handleSize: "115%",
                  handleStyle: {
                    color: colors.surface,
                    borderColor: colors.indexSeries[0],
                    borderWidth: 2,
                  },
                  moveHandleSize: 7,
                  moveHandleStyle: {
                    color: colors.indexSeries[0],
                    opacity: 0.45,
                  },
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
                {
                  name: histogramLabel,
                  type: "bar",
                  xAxisIndex: 2,
                  yAxisIndex: 2,
                  data: histogramValues.map(value => ({
                    value,
                    itemStyle: {
                      color:
                        value === null || value >= 0 ? colors.up : colors.down,
                      opacity: 0.55,
                    },
                  })),
                },
                {
                  name: macdLabel,
                  type: "line",
                  xAxisIndex: 2,
                  yAxisIndex: 2,
                  data: macdValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[0] },
                },
                {
                  name: signalLabel,
                  type: "line",
                  xAxisIndex: 2,
                  yAxisIndex: 2,
                  data: signalValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[1] },
                },
                {
                  name: rsiLabel,
                  type: "line",
                  xAxisIndex: 3,
                  yAxisIndex: 3,
                  data: rsiValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[3] },
                  markLine: {
                    silent: true,
                    symbol: "none",
                    label: { show: false },
                    lineStyle: { color: colors.grid, type: "dashed" },
                    data: [{ yAxis: 30 }, { yAxis: 70 }],
                  },
                },
              ],
            }}
          />
        </ClientOnly>
      </div>
      <div className="sr-only">
        <table>
          <caption>{t("indexChartAccessibleSummary")}</caption>
          <thead>
            <tr>
              <th>{t("indexChartDate")}</th>
              <th>{closeLabel}</th>
              <th>{activityLabel}</th>
              <th>{macdLabel}</th>
              <th>{signalLabel}</th>
              <th>{histogramLabel}</th>
              <th>{rsiLabel}</th>
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
                <td>{formatNumber(macdValues[index], null, locale)}</td>
                <td>{formatNumber(signalValues[index], null, locale)}</td>
                <td>{formatNumber(histogramValues[index], null, locale)}</td>
                <td>{formatNumber(rsiValues[index], null, locale)}</td>
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
