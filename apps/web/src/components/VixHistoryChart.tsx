import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { formatIsoDate, formatNumber, numberLocales } from "#/lib/format"
import type { VixHistory } from "#/lib/indices"

export function VixHistoryLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel mt-6 animate-pulse p-5"
      role="status"
      aria-live="polite"
      aria-label={t("vixChartLoading")}
    >
      <div className="h-5 w-52 rounded bg-line" />
      <div className="mt-5 h-84 rounded-lg bg-line sm:h-96" />
    </section>
  )
}

function VixState({ message }: { message: string }) {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel mt-6 p-5"
      role="status"
      aria-live="polite"
    >
      <h2 className="m-0 text-base font-extrabold">{t("vixChartTitle")}</h2>
      <p className="mt-3 mb-0 text-sm text-sea-ink-soft">{message}</p>
    </section>
  )
}

export function VixHistoryChart({
  history,
  locale,
}: {
  history: VixHistory | null
  locale: Locale
}) {
  const { t } = useTranslation()
  const colors = useChartColors()

  if (history === null) {
    return <VixState message={t("vixChartUnavailable")} />
  }
  if (history.bars.length === 0) {
    return <VixState message={t("vixChartEmpty")} />
  }

  const latest = history.bars.at(-1)!
  const dates = history.bars.map(bar => bar.trade_date)
  const values = history.bars.map(bar => Number(bar.close))
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const chartMinimum = Math.max(0, Math.min(15, Math.floor(minimum - 2)))
  const chartMaximum = Math.max(35, Math.ceil(maximum + 2))
  const closeLabel = t("vixChartClose")
  const macdLabel = t("indexChartMacd")
  const signalLabel = t("indexChartMacdSignal")
  const histogramLabel = t("indexChartMacdHistogram")
  const kdLabel = t("indexChartKd")
  const kLabel = t("indexChartK")
  const dLabel = t("indexChartD")
  const macdByDate = new Map(
    (history.indicators?.macd.points ?? []).map(point => [
      point.trade_date,
      point,
    ])
  )
  const kdByDate = new Map(
    (history.indicators?.kd.points ?? []).map(point => [
      point.trade_date,
      point,
    ])
  )
  const macdValues = dates.map(date => {
    const value = macdByDate.get(date)?.macd
    return value === null || value === undefined ? null : Number(value)
  })
  const signalValues = dates.map(date => {
    const value = macdByDate.get(date)?.signal
    return value === null || value === undefined ? null : Number(value)
  })
  const histogramValues = dates.map(date => {
    const value = macdByDate.get(date)?.histogram
    return value === null || value === undefined ? null : Number(value)
  })
  const kValues = dates.map(date => {
    const value = kdByDate.get(date)?.k
    return value === null || value === undefined ? null : Number(value)
  })
  const dValues = dates.map(date => {
    const value = kdByDate.get(date)?.d
    return value === null || value === undefined ? null : Number(value)
  })

  return (
    <section
      className="surface-panel mt-6 min-w-0 p-5"
      aria-labelledby="vix-history-title"
    >
      <div className="mb-4 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 id="vix-history-title" className="m-0 text-base font-extrabold">
            {t("vixChartTitle")}
          </h2>
          <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
            {t("vixChartRange", {
              start: formatIsoDate(history.start, locale),
              end: formatIsoDate(history.end, locale),
            })}
          </p>
        </div>
        <p className="m-0 text-xs font-semibold text-sea-ink-soft">
          {t("vixChartBandsAreReference")}
        </p>
      </div>
      <div className="h-144 min-w-0 w-full overflow-hidden border-y border-line py-2 sm:h-160">
        <ClientOnly
          fallback={
            <div
              className="h-full w-full animate-pulse rounded-lg bg-link-hover"
              role="status"
              aria-label={t("vixChartLoading")}
            />
          }
        >
          <ReactECharts
            style={{ height: "100%", width: "100%" }}
            option={{
              animation: false,
              aria: {
                enabled: true,
                description: t("vixChartAccessibleSummary"),
              },
              title: [
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
                  coord: [0, 4],
                },
                {
                  text: kdLabel,
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
              ],
              matrix: {
                left: 0,
                right: 0,
                top: 4,
                bottom: 54,
                x: { show: false, data: [null] },
                y: { show: false, data: Array(8).fill(null) },
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
                        [6, 7],
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
                  top: 36,
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
              ],
              axisPointer: { link: [{ xAxisIndex: "all" }] },
              tooltip: {
                trigger: "axis",
                appendToBody: true,
                valueFormatter: (value: number | string) =>
                  formatNumber(value, "index", locale),
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
                  min: chartMinimum,
                  max: chartMaximum,
                  axisLabel: { color: colors.text },
                  splitLine: {
                    lineStyle: { color: colors.grid, type: "dashed" },
                  },
                },
                {
                  type: "value",
                  gridIndex: 1,
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
                  gridIndex: 2,
                  min: 0,
                  max: 100,
                  interval: 20,
                  axisLabel: { color: colors.text },
                  splitLine: {
                    lineStyle: { color: colors.grid, type: "dashed" },
                  },
                },
              ],
              dataZoom: [
                {
                  type: "inside",
                  xAxisIndex: [0, 1, 2],
                  start: 0,
                  end: 100,
                },
                {
                  type: "slider",
                  xAxisIndex: [0, 1, 2],
                  start: 0,
                  end: 100,
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
                {
                  name: closeLabel,
                  type: "line",
                  xAxisIndex: 0,
                  yAxisIndex: 0,
                  data: values,
                  connectNulls: false,
                  showSymbol: false,
                  lineStyle: { width: 3 },
                  areaStyle: { color: "transparent" },
                  markLine: {
                    silent: true,
                    symbol: "none",
                    label: { color: colors.text },
                    lineStyle: { color: colors.text, type: "dashed" },
                    data: [
                      { name: t("vixChartThresholdElevated"), yAxis: 20 },
                      { name: t("vixChartThresholdHigh"), yAxis: 30 },
                    ],
                  },
                  markArea: {
                    silent: true,
                    label: { color: colors.text, fontSize: 11 },
                    data: [
                      [
                        {
                          name: t("vixChartBandCalm"),
                          yAxis: chartMinimum,
                          itemStyle: {
                            color: colors.vixRisk.calm,
                            opacity: 0.08,
                          },
                        },
                        { yAxis: 20 },
                      ],
                      [
                        {
                          name: t("vixChartBandElevated"),
                          yAxis: 20,
                          itemStyle: {
                            color: colors.vixRisk.elevated,
                            opacity: 0.1,
                          },
                        },
                        { yAxis: 30 },
                      ],
                      [
                        {
                          name: t("vixChartBandHigh"),
                          yAxis: 30,
                          itemStyle: {
                            color: colors.vixRisk.high,
                            opacity: 0.08,
                          },
                        },
                        { yAxis: chartMaximum },
                      ],
                    ],
                  },
                },
                {
                  name: histogramLabel,
                  type: "bar",
                  xAxisIndex: 1,
                  yAxisIndex: 1,
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
                  xAxisIndex: 1,
                  yAxisIndex: 1,
                  data: macdValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[0] },
                },
                {
                  name: signalLabel,
                  type: "line",
                  xAxisIndex: 1,
                  yAxisIndex: 1,
                  data: signalValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[1] },
                },
                {
                  name: kLabel,
                  type: "line",
                  xAxisIndex: 2,
                  yAxisIndex: 2,
                  data: kValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[2] },
                  markLine: {
                    silent: true,
                    symbol: "none",
                    label: { show: false },
                    lineStyle: { color: colors.grid, type: "dashed" },
                    data: [{ yAxis: 20 }, { yAxis: 80 }],
                  },
                },
                {
                  name: dLabel,
                  type: "line",
                  xAxisIndex: 2,
                  yAxisIndex: 2,
                  data: dValues,
                  showSymbol: false,
                  connectNulls: false,
                  lineStyle: { width: 1.5, color: colors.series[3] },
                },
              ],
            }}
          />
        </ClientOnly>
      </div>
      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs text-sea-ink-soft">
        <div className="flex gap-1">
          <dt>{t("vixChartLatestClose")}</dt>
          <dd className="m-0 font-mono font-semibold text-sea-ink tabular-nums">
            {formatNumber(latest.close, "index", locale)}
          </dd>
        </div>
      </dl>
      <div className="sr-only">
        <table>
          <caption>{t("vixChartAccessibleSummary")}</caption>
          <thead>
            <tr>
              <th>{t("vixChartDate")}</th>
              <th>{closeLabel}</th>
              <th>{macdLabel}</th>
              <th>{signalLabel}</th>
              <th>{histogramLabel}</th>
              <th>{kLabel}</th>
              <th>{dLabel}</th>
            </tr>
          </thead>
          <tbody>
            {history.bars.map((bar, index) => (
              <tr key={bar.trade_date}>
                <th>{formatIsoDate(bar.trade_date, locale)}</th>
                <td>{formatNumber(bar.close, "index", locale)}</td>
                <td>{formatNumber(macdValues[index], null, locale)}</td>
                <td>{formatNumber(signalValues[index], null, locale)}</td>
                <td>{formatNumber(histogramValues[index], null, locale)}</td>
                <td>{formatNumber(kValues[index], null, locale)}</td>
                <td>{formatNumber(dValues[index], null, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
