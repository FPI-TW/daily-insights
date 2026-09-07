import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { z } from "zod"
import type { IndexMovingAverages, Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { formatDateStamp, formatNumber, numberLocales } from "#/lib/format"
import {
  biasSeries,
  indexNameKey,
  indexWindowStart,
  scaleBias,
  visibleBiasPoints,
  type IndexHistorySeries,
  type IndexMovingAverageMap,
  type MarketIndexHistory,
} from "#/lib/indices"
import {
  DashboardChoices,
  DashboardPanel,
  DashboardSection,
  Methodology,
} from "./DashboardPrimitives"

function ChartSkeleton({ candles = false }: { candles?: boolean }) {
  const { t } = useTranslation()
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={t("indexChartLoading")}
      className="mt-4 animate-pulse motion-reduce:animate-none"
    >
      {candles ? (
        <>
          <div className="h-78 rounded bg-line/60" />
          <div className="mt-2.5 h-21 rounded bg-line/60" />
        </>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-px bg-line">
            {[0, 1, 2].map(i => (
              <div key={i} className="h-28 bg-surface px-4 py-3.5">
                <div className="h-full rounded bg-line/60" />
              </div>
            ))}
          </div>
          <div className="mt-4.5 h-65 rounded bg-line/60" />
        </>
      )}
    </div>
  )
}
export function TaiwanIndexHistoryLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="mt-6 space-y-4"
      role="status"
      aria-live="polite"
      aria-label={t("indexChartLoading")}
    >
      <div className="rounded-2xl border border-line bg-surface p-5">
        <div className="h-5 w-44 rounded bg-line" />
        <div className="mt-4 grid grid-cols-3 gap-1">
          {[0, 1, 2].map(i => (
            <div key={i} className="h-28 animate-pulse rounded bg-line/60" />
          ))}
        </div>
        <div className="mt-4.5 h-65 animate-pulse rounded bg-line/60" />
      </div>
      <div className="rounded-2xl border border-line bg-surface p-5">
        <div className="h-78 animate-pulse rounded bg-line/60" />
        <div className="mt-2.5 h-21 animate-pulse rounded bg-line/60" />
      </div>
    </section>
  )
}
function Unavailable() {
  const { t } = useTranslation()
  return (
    <p
      role="status"
      className="flex min-h-48 items-center justify-center text-sm text-sea-ink-soft"
    >
      {t("indexChartUnavailable")}
    </p>
  )
}
function signed(value: number | null, locale: Locale, digits = 2) {
  if (value === null) return "—"
  return `${new Intl.NumberFormat(numberLocales[locale], { minimumFractionDigits: digits, maximumFractionDigits: digits, signDisplay: "exceptZero" }).format(Math.abs(value) < 0.005 ? 0 : value)}%`
}
function BiasValue({
  value,
  locale,
}: {
  value: number | null
  locale: Locale
}) {
  return (
    <span
      className={`font-mono text-xs font-bold tabular-nums ${value === null || Math.abs(value) < 0.005 ? "text-sea-ink-soft" : value > 0 ? "text-market-up" : "text-market-down"}`}
    >
      {signed(value, locale)}
    </span>
  )
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

function BiasPanel({
  selected,
  averages,
  pending,
  locale,
}: {
  selected: IndexHistorySeries
  averages: IndexMovingAverages | undefined
  pending: boolean
  locale: Locale
}) {
  const { t } = useTranslation()
  const colors = useChartColors()
  const [months, setMonths] = useState<12 | 18 | 24>(18)
  const [zoom, setZoom] = useState({ start: 0, end: 100 })
  const all = useMemo(
    () => biasSeries(selected.bars, averages),
    [selected.bars, averages]
  )
  const last = selected.bars.at(-1)!.trade_date
  const start = indexWindowStart(last, months)
  const lines = useMemo(
    () =>
      all.map(line => ({
        ...line,
        points: line.points.filter(point => point.date >= start),
      })),
    [all, start]
  )
  const dates = lines[0]!.points.map(point => point.date)
  const palette = [colors.indexSeries[0], colors.series[2], colors.series[1]]
  const visible = lines.map(line =>
    visibleBiasPoints(line.points, zoom.start, zoom.end)
  )
  const asOf = visible[0]?.at(-1)?.date
  const ready = lines.some(line =>
    line.points.some(point => point.value !== null)
  )
  const title =
    selected.symbol === "^TWII"
      ? t("biasTitle")
      : t("biasTitleIndex", {
          index: t(indexNameKey(selected.symbol) ?? selected.symbol),
        })
  return (
    <DashboardPanel
      title={title}
      controls={
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-sea-ink-soft">
            {t("windowLabel")}
          </span>
          <DashboardChoices
            label={t("windowLabel")}
            value={months}
            onChange={value => {
              setMonths(value)
              setZoom({ start: 0, end: 100 })
            }}
            options={([12, 18, 24] as const).map(value => ({
              value,
              label: t(`window_${value}`),
            }))}
          />
        </div>
      }
    >
      <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
        {t("biasSub", { window: t(`window_${months}`) })}
      </p>
      {pending ? (
        <ChartSkeleton />
      ) : !ready ? (
        <Unavailable />
      ) : (
        <>
          <div className="mt-3.5 flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-xs font-bold">{t("biasScaledTitle")}</span>
            <span className="text-[11px] text-sea-ink-soft">
              {t("biasScaledNote")}
            </span>
          </div>
          <p className="mt-1 mb-2 font-mono text-[11px] text-sea-ink-soft tabular-nums">
            {t("biasAsOf", { date: asOf ? formatDateStamp(asOf) : "—" })}
          </p>
          <div className="grid grid-cols-3 gap-px border-y border-line bg-line">
            {lines.map((line, i) => {
              const scaled = scaleBias(visible[i]!)
              return (
                <div
                  key={line.period}
                  className="min-w-0 bg-surface px-4 py-3.5"
                  data-testid={`bias-scaled-${line.period}`}
                >
                  <div className="flex items-center gap-1.5 text-xs text-sea-ink-soft">
                    <span
                      className="size-2.5 rounded-xs"
                      style={{ backgroundColor: palette[i] }}
                    />
                    {t(`bias_${line.period}`)}
                  </div>
                  <div className="mt-1 flex flex-wrap items-baseline gap-2">
                    <strong className="font-mono text-[21px] tracking-tight tabular-nums">
                      {scaled.value === null ? "—" : scaled.value.toFixed(0)}
                    </strong>
                    <BiasValue value={scaled.current} locale={locale} />
                  </div>
                  <div
                    className="relative mt-2 h-1 rounded-full bg-line-soft"
                    role="meter"
                    aria-label={t(`bias_${line.period}`)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={scaled.value ?? undefined}
                    aria-valuetext={
                      scaled.value === null
                        ? t("indexChartUnavailable")
                        : signed(scaled.current, locale)
                    }
                  >
                    {scaled.value !== null ? (
                      <>
                        <span
                          className="absolute inset-y-0 left-0 rounded-full opacity-35"
                          style={{
                            width: `${scaled.value}%`,
                            backgroundColor: palette[i],
                          }}
                        />
                        <span
                          className="absolute top-1/2 size-2.25 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 bg-surface"
                          style={{
                            left: `${scaled.value}%`,
                            borderColor: palette[i],
                          }}
                        />
                      </>
                    ) : null}
                  </div>
                  <div className="mt-1.5 flex justify-between gap-2 font-mono text-[10px] text-sea-ink-soft tabular-nums">
                    <span>{signed(scaled.min, locale, 1)}</span>
                    <span>{signed(scaled.max, locale, 1)}</span>
                  </div>
                </div>
              )
            })}
          </div>
          <div className="mt-4.5" role="img" aria-label={title}>
            <ClientOnly fallback={<ChartSkeleton />}>
              <ReactECharts
                notMerge
                style={{ height: 330 }}
                onEvents={{
                  datazoom: (event: unknown) => {
                    const parsed = zoomEventSchema.safeParse(event)
                    if (!parsed.success) return
                    const range =
                      "batch" in parsed.data
                        ? parsed.data.batch[0]!
                        : parsed.data
                    if (range.start <= range.end) setZoom(range)
                  },
                }}
                option={{
                  animation: false,
                  color: palette,
                  textStyle: { fontFamily: colors.font },
                  aria: { enabled: true, description: title },
                  grid: { left: 52, right: 20, top: 8, height: 234 },
                  tooltip: {
                    trigger: "axis",
                    appendToBody: true,
                    renderMode: "richText",
                    valueFormatter: (value: number | null) =>
                      signed(value, locale),
                  },
                  legend: {
                    bottom: 36,
                    itemWidth: 10,
                    itemHeight: 10,
                    icon: "rect",
                    itemGap: 20,
                    textStyle: { color: colors.text, fontSize: 12 },
                  },
                  xAxis: {
                    type: "category",
                    data: dates,
                    boundaryGap: false,
                    axisLine: { lineStyle: { color: colors.grid } },
                    axisLabel: {
                      color: colors.text,
                      fontFamily: "monospace",
                      fontSize: 11,
                      hideOverlap: true,
                      showMinLabel: true,
                      showMaxLabel: true,
                      interval: Math.max(
                        0,
                        Math.ceil((visible[0]!.length - 1) / 6) - 1
                      ),
                      formatter: (date: string) => date.slice(0, 7),
                    },
                  },
                  yAxis: {
                    type: "value",
                    axisLabel: {
                      color: colors.text,
                      fontFamily: "monospace",
                      fontSize: 11,
                      formatter: (value: number) => signed(value, locale, 1),
                    },
                    splitLine: {
                      lineStyle: { color: colors.gridSoft, type: "dashed" },
                    },
                  },
                  dataZoom: [
                    { type: "inside", ...zoom },
                    {
                      type: "slider",
                      ...zoom,
                      height: 18,
                      bottom: 0,
                      borderColor: colors.grid,
                      textStyle: { color: colors.text },
                    },
                  ],
                  series: lines.map((line, i) => ({
                    name: t(`bias_${line.period}`),
                    type: "line",
                    data: line.points.map(p => p.value),
                    connectNulls: false,
                    showSymbol: false,
                    lineStyle: { width: 1.6 },
                    ...(i === 0
                      ? {
                          markLine: {
                            symbol: "none",
                            silent: true,
                            label: { show: false },
                            lineStyle: {
                              color: colors.chipLine,
                              width: 1,
                              type: "solid",
                            },
                            data: [{ yAxis: 0 }],
                          },
                        }
                      : {}),
                  })),
                }}
              />
            </ClientOnly>
          </div>
        </>
      )}
      <Methodology>{t("biasFormula")}</Methodology>
    </DashboardPanel>
  )
}
function CandlesPanel({
  selected,
  averages,
  pending,
  locale,
}: {
  selected: IndexHistorySeries
  averages: IndexMovingAverages | undefined
  pending: boolean
  locale: Locale
}) {
  const { t } = useTranslation()
  const colors = useChartColors()
  const [zoom, setZoom] = useState({ start: 0, end: 100 })
  const bars = selected.bars
  const latest = bars.at(-1)!
  const previous = bars.at(-2)
  const change = previous
    ? (Number(latest.close) / Number(previous.close) - 1) * 100
    : null
  const dates = bars.map(bar => bar.trade_date)
  const lastDateIndex = dates.length - 1
  const visibleStart = dates[Math.round((lastDateIndex * zoom.start) / 100)]
  const visibleEnd = dates[Math.round((lastDateIndex * zoom.end) / 100)]
  const missing = bars.filter(
    bar => bar.open === null || bar.high === null || bar.low === null
  ).length
  const name = t(indexNameKey(selected.symbol) ?? selected.symbol)
  const title =
    selected.symbol === "^TWII"
      ? t("kTitle")
      : t("kTitleIndex", { index: name })
  const volume = (value: number | null) =>
    value === null
      ? "—"
      : new Intl.NumberFormat(numberLocales[locale], {
          maximumFractionDigits: 2,
        }).format(value / 100_000_000)
  const candleData = bars.map(bar =>
    bar.open === null || bar.high === null || bar.low === null
      ? [null, null, null, null]
      : [Number(bar.open), Number(bar.close), Number(bar.low), Number(bar.high)]
  )
  const maLines = [20, 60].flatMap(period => {
    const series = averages?.series.find(series => series.period === period)
    if (!series?.points.some(p => p.value !== null)) return []
    const byDate = new Map(series.points.map(p => [p.trade_date, p.value]))
    return [
      {
        name: t(`ma_${period}`),
        type: "line",
        data: bars.map(bar => {
          const value = byDate.get(bar.trade_date)
          return value == null ? null : Number(value)
        }),
        connectNulls: false,
        showSymbol: false,
        lineStyle: {
          width: 1.6,
          color: period === 20 ? colors.series[2] : colors.series[1],
        },
        itemStyle: {
          color: period === 20 ? colors.series[2] : colors.series[1],
        },
      },
    ]
  })
  const volumes = bars.map(bar =>
    bar.volume === null ? null : bar.volume / 100_000_000
  )
  const maximum = Math.max(1, ...volumes.flatMap(v => (v === null ? [] : [v])))
  const magnitude = 10 ** Math.floor(Math.log10(maximum))
  const volumeMax = Math.ceil(maximum / magnitude) * magnitude
  return (
    <DashboardPanel
      title={title}
      controls={
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-sea-ink-soft">
          <span>{t("kClose")}</span>
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
                : signed(Math.abs(change), locale).replace("+", "")}
            </span>
          </span>
          <span>{t("volumeLabel")}</span>
          <strong className="font-mono text-[13px] text-sea-ink tabular-nums">
            {volume(latest.volume)}
          </strong>
        </div>
      }
    >
      <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
        {t("kSub")} ·{" "}
        <span className="font-mono tabular-nums">
          {formatDateStamp(latest.trade_date)}
        </span>
      </p>
      {missing ? (
        <p className="mt-2 mb-0 text-xs text-sea-ink-soft" role="status">
          {t("kMissingOhlc", { count: missing })}
        </p>
      ) : null}
      {pending ? (
        <p
          className="mt-2 mb-0 text-xs text-sea-ink-soft"
          role="status"
          aria-live="polite"
        >
          {t("indexMaLoading")}
        </p>
      ) : null}
      {missing === bars.length ? (
        <Unavailable />
      ) : (
        <div className="mt-3" role="img" aria-label={title}>
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-sea-ink-soft">
            <span>{t("kVisibleRange")}</span>
            <span className="font-mono tabular-nums">
              {visibleStart && visibleEnd
                ? `${formatDateStamp(visibleStart)} – ${formatDateStamp(visibleEnd)}`
                : "—"}
            </span>
          </div>
          <ClientOnly fallback={<ChartSkeleton candles />}>
            <ReactECharts
              notMerge
              style={{ height: 522 }}
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
                textStyle: { fontFamily: colors.font },
                aria: { enabled: true, description: title },
                grid: [
                  { left: 64, right: 20, top: 8, height: 312 },
                  { left: 64, right: 20, top: 330, height: 84 },
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
                    const bar = bars[parsed.data[0].dataIndex]
                    if (!bar) return ""
                    return [
                      bar.trade_date,
                      `${t("kOpen")}: ${formatNumber(bar.open, null, locale)}`,
                      `${t("kHigh")}: ${formatNumber(bar.high, null, locale)}`,
                      `${t("kLow")}: ${formatNumber(bar.low, null, locale)}`,
                      `${t("kClose")}: ${formatNumber(bar.close, null, locale)}`,
                      `${t("volumeLabel")}: ${volume(bar.volume)}`,
                      ...maLines.map(
                        line =>
                          `${line.name}: ${formatNumber(line.data[parsed.data[0]!.dataIndex], null, locale)}`
                      ),
                    ].join("\n")
                  },
                },
                legend: {
                  bottom: 52,
                  data: [...maLines.map(line => line.name), t("volumeLegend")],
                  itemWidth: 10,
                  itemHeight: 10,
                  icon: "rect",
                  itemGap: 20,
                  textStyle: { color: colors.text, fontSize: 12 },
                },
                xAxis: [
                  {
                    type: "category",
                    data: dates,
                    gridIndex: 0,
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
                      fontFamily: "monospace",
                      fontSize: 11,
                      hideOverlap: true,
                      showMinLabel: true,
                      showMaxLabel: true,
                      interval: Math.max(
                        0,
                        Math.ceil((dates.length - 1) / 6) - 1
                      ),
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
                    axisLabel: {
                      color: colors.text,
                      fontFamily: "monospace",
                      fontSize: 11,
                      formatter: (value: number) =>
                        new Intl.NumberFormat(numberLocales[locale], {
                          maximumFractionDigits: 0,
                        }).format(value),
                    },
                    splitLine: {
                      lineStyle: { color: colors.gridSoft, type: "dashed" },
                    },
                  },
                  {
                    type: "value",
                    gridIndex: 1,
                    min: 0,
                    max: volumeMax,
                    interval: volumeMax,
                    axisLabel: {
                      color: colors.text,
                      fontFamily: "monospace",
                      fontSize: 11,
                      showMinLabel: false,
                      verticalAlign: "top",
                      padding: [2, 0, 0, 0],
                    },
                    splitLine: {
                      lineStyle: { color: colors.gridSoft, type: "dashed" },
                    },
                  },
                ],
                dataZoom: [
                  { type: "inside", xAxisIndex: [0, 1], ...zoom },
                  {
                    type: "slider",
                    xAxisIndex: [0, 1],
                    ...zoom,
                    height: 26,
                    bottom: 6,
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
                    name: t("kClose"),
                    type: "candlestick",
                    data: candleData,
                    barMaxWidth: 6,
                    itemStyle: {
                      color: colors.up,
                      color0: colors.down,
                      borderColor: colors.up,
                      borderColor0: colors.down,
                    },
                  },
                  ...maLines,
                  {
                    name: t("volumeLegend"),
                    type: "bar",
                    xAxisIndex: 1,
                    yAxisIndex: 1,
                    itemStyle: { color: colors.up, opacity: 0.4 },
                    data: bars.map((bar, i) => ({
                      value: volumes[i],
                      itemStyle: {
                        color:
                          bar.open === null
                            ? colors.faint
                            : Number(bar.close) >= Number(bar.open)
                              ? colors.up
                              : colors.down,
                      },
                    })),
                  },
                ],
              }}
            />
          </ClientOnly>
        </div>
      )}
      <div className="sr-only">
        <table>
          <caption>{title}</caption>
          <thead>
            <tr>
              <th>{t("indexChartDate")}</th>
              <th>{t("kOpen")}</th>
              <th>{t("kHigh")}</th>
              <th>{t("kLow")}</th>
              <th>{t("kClose")}</th>
              <th>{t("volumeLabel")}</th>
              {maLines.map(line => (
                <th key={line.name}>{line.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {bars.map((bar, i) => (
              <tr key={bar.trade_date}>
                <th>{bar.trade_date}</th>
                <td>{formatNumber(bar.open, null, locale)}</td>
                <td>{formatNumber(bar.high, null, locale)}</td>
                <td>{formatNumber(bar.low, null, locale)}</td>
                <td>{formatNumber(bar.close, null, locale)}</td>
                <td>{volume(bar.volume)}</td>
                {maLines.map(line => (
                  <td key={line.name}>
                    {formatNumber(line.data[i], null, locale)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Methodology>{t("kFormula", { symbol: selected.symbol })}</Methodology>
    </DashboardPanel>
  )
}
export function TaiwanIndexHistoryChart({
  history,
  locale,
  movingAverages = null,
}: {
  history: MarketIndexHistory | null
  locale: Locale
  movingAverages?: Promise<IndexMovingAverageMap> | null
}) {
  const { t } = useTranslation()
  const [selectedSymbol, setSelectedSymbol] = useState("")
  const [state, setState] = useState<{
    source: Promise<IndexMovingAverageMap> | null
    values: IndexMovingAverageMap
  }>({ source: null, values: {} })
  useEffect(() => {
    let active = true
    const source = movingAverages
    if (source)
      void source.then(
        values => {
          if (active) setState({ source, values })
        },
        () => {
          if (active) setState({ source, values: {} })
        }
      )
    return () => {
      active = false
    }
  }, [movingAverages])
  const selected =
    history?.series.find(item => item.symbol === selectedSymbol) ??
    history?.series[0]
  if (!history || !selected?.bars.length)
    return (
      <section
        className="mt-6 rounded-2xl border border-line bg-surface p-5"
        role="status"
      >
        <h2 className="m-0 text-lg font-extrabold">{t("sectionTechnical")}</h2>
        <p className="mt-3 mb-0 text-sm text-sea-ink-soft">
          {history && !history.failedSymbols.length
            ? t("indexChartEmpty")
            : t("indexChartUnavailable")}
        </p>
      </section>
    )
  const pending = movingAverages !== null && state.source !== movingAverages
  const averages =
    state.source === movingAverages ? state.values[selected.symbol] : undefined
  const name = t(indexNameKey(selected.symbol) ?? selected.symbol)
  return (
    <div className="mt-6 min-w-0">
      <DashboardSection
        number="01"
        title={t("sectionTechnical")}
        meta={t("techMeta", { index: name, symbol: selected.symbol })}
      >
        {history.series.length > 1 ? (
          <label className="mb-3 ml-auto max-w-72 text-xs font-bold text-sea-ink-soft">
            {t("indexChartSelect")}
            <select
              value={selected.symbol}
              onChange={event => setSelectedSymbol(event.target.value)}
            >
              {history.series.map(item => (
                <option key={item.symbol} value={item.symbol}>
                  {t(indexNameKey(item.symbol) ?? item.symbol)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {history.failedSymbols.length ? (
          <p
            role="status"
            className="mb-4 rounded-md border border-market-caution/40 bg-market-caution/10 p-3 text-sm text-sea-ink-soft"
          >
            {t("indexChartPartial", {
              symbols: history.failedSymbols.join(", "),
            })}
          </p>
        ) : null}
        <div className="space-y-4">
          <BiasPanel
            key={selected.symbol}
            selected={selected}
            averages={averages}
            pending={pending}
            locale={locale}
          />
          <CandlesPanel
            key={`candles-${selected.symbol}`}
            selected={selected}
            averages={averages}
            pending={pending}
            locale={locale}
          />
        </div>
        <p className="mt-5 max-w-[100ch] text-xs leading-5 text-pretty text-sea-ink-soft">
          {t("footNote", { symbol: selected.symbol })}
        </p>
      </DashboardSection>
    </div>
  )
}
