import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { z } from "zod"
import type { IndexMovingAverages, Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { numberLocales } from "#/lib/format"
import {
  biasSeries,
  indexDisplayName,
  indexWindowStart,
  scaleBias,
  visibleBiasPoints,
  type IndexHistorySeries,
} from "#/lib/indices"
import { DashboardChoices, DashboardPanel } from "./DashboardPrimitives"

export function ChartSkeleton({ candles = false }: { candles?: boolean }) {
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
export function Unavailable() {
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
export function signed(value: number | null, locale: Locale, digits = 2) {
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
export const zoomEventSchema = z.union([
  zoomRangeSchema,
  z.object({ batch: z.array(zoomRangeSchema).min(1) }),
])
/** 20/60/120MA bias for one index; shared by the Taiwan and index charts. */
export function IndexBiasPanel({
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
  const [months, setMonths] = useState<12 | 18>(18)
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
  const ready = lines.some(line =>
    line.points.some(point => point.value !== null)
  )
  const title =
    selected.symbol === "^TWII"
      ? t("biasTitle")
      : t("biasTitleIndex", {
          index: indexDisplayName(selected.symbol, t),
        })
  return (
    <DashboardPanel
      title={title}
      controls={
        <div className="flex flex-wrap items-center gap-2">
          <DashboardChoices
            label={t("windowLabel")}
            value={months}
            onChange={value => {
              setMonths(value)
              setZoom({ start: 0, end: 100 })
            }}
            options={([12, 18] as const).map(value => ({
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
    </DashboardPanel>
  )
}
