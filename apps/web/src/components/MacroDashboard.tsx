import { ResponsiveTable } from "./ResponsiveTable"
import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { numberLocales, unitLabel } from "#/lib/format"
import {
  alignedRatioAxes,
  periodChange,
  ratioPoints,
  recentPoints,
  yieldCurve,
  type MacroDashboardData,
  type MacroHistory,
  type Period,
} from "#/lib/macro-dashboard"
import { DashboardChoices, DashboardPanel } from "./DashboardPrimitives"

const commodityIds = ["brent", "wti", "gold", "silver", "copper"]
const fxIds = [
  "eur_usd",
  "gbp_usd",
  "aud_usd",
  "nzd_usd",
  "usd_jpy",
  "usd_chf",
  "usd_cad",
  "usd_twd",
]
const tenorIds = ["3m", "2y", "5y", "10y", "30y"]
const periods = ["day", "week", "month", "year"] as const

export function MacroDashboardLoading() {
  const { t } = useTranslation()
  return (
    <div role="status" aria-live="polite" className="space-y-9">
      <span className="sr-only">{t("macroLoading")}</span>
      {[0, 1, 2].map(i => (
        <div
          key={i}
          className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,560px),1fr))] gap-6"
        >
          {[0, 1].map(j => (
            <div
              key={j}
              className="h-72 animate-pulse rounded-2xl border border-line bg-surface p-5 motion-reduce:animate-none"
            >
              <div className="mb-8 h-5 w-48 rounded bg-line" />
              <div className="h-44 rounded bg-line/60" />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
function Unavailable() {
  const { t } = useTranslation()
  return (
    <p
      className="flex min-h-48 items-center justify-center text-sm text-sea-ink-soft"
      role="status"
    >
      {t("macroUnavailable")}
    </p>
  )
}
function formatValue(
  value: number | null | undefined,
  unit: string | undefined,
  locale: Locale
) {
  if (value == null) return "—"
  return new Intl.NumberFormat(numberLocales[locale], {
    minimumFractionDigits: unit === "percent" ? 2 : 0,
    maximumFractionDigits:
      unit === "ratio"
        ? 6
        : unit === "percent" || Math.abs(value) >= 10
          ? 2
          : 4,
  }).format(value)
}
function Change({
  value,
  locale,
  rates = false,
}: {
  value: number | null
  locale: Locale
  rates?: boolean
}) {
  const flat = value !== null && Math.abs(value) < 0.005
  return (
    <span
      className={`inline-flex max-w-full flex-wrap items-baseline justify-end gap-x-2 gap-y-1 font-mono font-bold tabular-nums text-sm ${value === null || flat ? "text-sea-ink-soft" : value > 0 ? "text-market-up" : "text-market-down"}`}
    >
      {value !== null && !flat ? <span>{value > 0 ? "▲" : "▼"}</span> : null}
      <span>
        {value === null
          ? "—"
          : `${new Intl.NumberFormat(numberLocales[locale], { maximumFractionDigits: 2 }).format(flat ? 0 : Math.abs(value))}${rates ? "" : "%"}`}
      </span>
      {value !== null && rates ? (
        <span className="font-sans text-xs">bp</span>
      ) : null}
    </span>
  )
}
function Range({
  value,
  onChange,
}: {
  value: number
  onChange: (value: number) => void
}) {
  const { t } = useTranslation()
  return (
    <DashboardChoices
      label={t("macroRange")}
      value={value}
      onChange={onChange}
      options={[30, 90, 365].map(value => ({
        value,
        label: t("macroDays", { count: value }),
      }))}
    />
  )
}
type ChartLine = {
  name: string
  unit: string | undefined
  points: { date: string; value: number | null }[]
  change?: number | null
}
function Chart({
  lines,
  label,
  locale,
  dual = false,
  curve = false,
  days = 90,
  height = 252,
}: {
  lines: ChartLine[]
  label: string
  locale: Locale
  dual?: boolean
  curve?: boolean
  days?: number
  height?: number
}) {
  const colors = useChartColors()
  const { t } = useTranslation()
  if (!lines.some(line => line.points.some(point => point.value !== null)))
    return <Unavailable />
  const dates = curve
    ? lines[0]!.points.map(point => point.date)
    : [
        ...new Set(lines.flatMap(line => line.points.map(point => point.date))),
      ].sort()
  const maps = lines.map(
    line => new Map(line.points.map(point => [point.date, point.value]))
  )
  const bounds = dual
    ? alignedRatioAxes(
        ...([0, 1].map(i =>
          lines[i]!.points.flatMap(p => (p.value === null ? [] : [p.value]))
        ) as [number[], number[]])
      )
    : []
  const axis = (index: number) => ({
    type: "value",
    scale: true,
    ...bounds[index],
    position: index === 0 ? "left" : "right",
    axisLabel: {
      color: colors.text,
      fontFamily: "monospace",
      fontSize: 11,
      formatter: (value: number) =>
        formatValue(value, lines[index]?.unit, locale),
    },
    splitLine: {
      show: index === 0,
      lineStyle: { color: colors.gridSoft, type: "dashed" },
    },
  })
  return (
    <>
      <div
        className="mt-3 flex flex-wrap items-baseline gap-x-4.5 gap-y-2"
        aria-label={t("macroLatestObservations")}
      >
        {lines.map((line, i) => {
          const latest = curve
            ? line.points.find(point => point.date === "10Y")
            : line.points.at(-1)
          return (
            <div
              key={line.name}
              className="flex flex-wrap items-baseline gap-x-3 gap-y-2 text-xs text-sea-ink-soft"
            >
              <span
                className="inline-block size-2.5 rounded-full"
                style={{
                  backgroundColor:
                    curve && i === 1 ? colors.faint : colors.series[i],
                }}
              />
              <span>
                {line.name}
                {curve ? " · 10Y" : ""}
              </span>
              <strong className="font-mono text-[15px] text-sea-ink tabular-nums">
                {formatValue(latest?.value, line.unit, locale)}
              </strong>
              <span>
                {line.unit ? (unitLabel(line.unit, t) ?? line.unit) : ""}
              </span>
              {line.change !== undefined ? (
                <Change value={line.change} locale={locale} />
              ) : null}
            </div>
          )
        })}
      </div>
      <div className="mt-3 min-w-0" role="img" aria-label={label}>
        <ClientOnly
          fallback={
            <div
              className="h-64 animate-pulse rounded bg-line/40"
              role="status"
              aria-label={t("macroLoading")}
            />
          }
        >
          <ReactECharts
            style={{ height }}
            notMerge
            option={{
              animation: false,
              textStyle: { fontFamily: colors.font },
              color: curve ? [colors.series[0], colors.faint] : colors.series,
              aria: { enabled: true, description: label },
              tooltip: {
                trigger: "axis",
                renderMode: "richText",
                axisPointer: { type: "line" },
              },
              grid: {
                top: 8,
                bottom: 36,
                left: dual ? 58 : curve ? 44 : 48,
                right: dual ? 62 : 20,
              },
              xAxis: {
                type: "category",
                data: dates,
                boundaryGap: false,
                axisLabel: {
                  color: colors.text,
                  fontFamily: "monospace",
                  fontSize: 11,
                  hideOverlap: true,
                  showMinLabel: true,
                  showMaxLabel: true,
                  interval: Math.max(0, Math.ceil((dates.length - 1) / 6) - 1),
                  formatter: (day: string) =>
                    curve ? day : days <= 200 ? day.slice(5) : day.slice(0, 7),
                },
                axisLine: { lineStyle: { color: colors.grid } },
              },
              yAxis: dual ? [axis(0), axis(1)] : axis(0),
              series: lines.map((line, i) => ({
                name: line.name,
                type: "line",
                showSymbol: curve && i === 0,
                symbol: "emptyCircle",
                symbolSize: 9,
                connectNulls: false,
                data: dates.map(date => maps[i]!.get(date) ?? null),
                lineStyle: {
                  width: 2,
                  type: curve && i === 1 ? [5, 4] : "solid",
                },
                itemStyle: { borderWidth: 2 },
                emphasis: { focus: "series" },
                ...(dual ? { yAxisIndex: i } : {}),
                tooltip: {
                  valueFormatter: (value: number | null) =>
                    `${formatValue(value, line.unit, locale)} ${line.unit ? (unitLabel(line.unit, t) ?? line.unit) : ""}`,
                },
              })),
            }}
          />
        </ClientOnly>
      </div>
    </>
  )
}
function HistoryTable({
  ids,
  histories,
  locale,
  rates = false,
  selected,
  onSelect,
  fetchedAt,
}: {
  ids: string[]
  histories: MacroHistory[]
  locale: Locale
  rates?: boolean
  selected?: string
  onSelect?: (id: string) => void
  fetchedAt: string | undefined
}) {
  const { t } = useTranslation()
  const fx = Boolean(onSelect)
  // Commodity closes carry their quote unit; FX and yields already say so.
  const units = !fx && !rates
  if (
    !ids.some(id =>
      histories.some(history => history.id === id && history.points.length)
    )
  )
    return <Unavailable />
  return (
    <div className="mt-4 min-w-0">
      <ResponsiveTable>
        <thead className="border-b border-line text-sea-ink-soft">
          <tr>
            <th
              scope="col"
              className={`px-2 py-1.5 text-left font-semibold ${units ? "@lg:w-[17%]!" : ""}`}
            >
              {t(rates ? "macroTenor" : "macroInstrument")}
            </th>
            <th
              scope="col"
              className={`whitespace-nowrap px-2 py-1.5 font-semibold ${units ? "@lg:w-[20%]" : ""}`}
            >
              {t(rates ? "macroYield" : "macroClose")}
            </th>
            {periods.map(period => (
              <th
                key={period}
                scope="col"
                className="whitespace-nowrap px-2 py-1.5 font-semibold"
              >
                {t(`macroPeriod_${period}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ids.map(id => {
            const history = histories.find(item => item.id === id)
            const latest = history?.points.at(-1)
            const stale =
              latest &&
              fetchedAt &&
              Date.parse(fetchedAt) - Date.parse(latest.date) > 7 * 86_400_000
            return (
              <tr
                key={id}
                className={`border-t border-line-soft ${selected === id ? "bg-lagoon/8" : ""}`}
              >
                <th
                  scope="row"
                  className="px-2 py-1 text-left font-semibold text-pretty text-sea-ink"
                >
                  {onSelect ? (
                    <button
                      type="button"
                      aria-pressed={selected === id}
                      onClick={() => onSelect(id)}
                      className={`border-0 bg-transparent p-0 text-left font-mono text-sm font-bold underline-offset-3 hover:underline hover:decoration-lagoon ${selected === id ? "text-palm" : "text-sea-ink"}`}
                    >
                      {t(`macroAsset_${id}`)}
                    </button>
                  ) : (
                    t(`macroAsset_${id}`)
                  )}
                  {stale ? (
                    <span className="block text-xs font-normal text-sea-ink-soft">
                      {t("reportStale")}
                    </span>
                  ) : null}
                </th>
                <td
                  data-label={t(rates ? "macroYield" : "macroClose")}
                  className="whitespace-nowrap px-2 py-1 text-right font-mono font-bold text-sea-ink"
                >
                  <span className="whitespace-nowrap">
                    {formatValue(
                      latest ? Number(latest.value) : null,
                      history?.unit,
                      locale
                    )}
                    {history && units ? (
                      <span className="ml-1 font-sans text-xs font-normal text-sea-ink-soft">
                        {unitLabel(history.unit, t) ?? history.unit}
                      </span>
                    ) : null}
                  </span>
                </td>
                {periods.map(period => (
                  <td
                    key={period}
                    data-label={t(`macroPeriod_${period}`)}
                    className="px-2 py-1 text-right"
                  >
                    <Change
                      value={
                        history ? periodChange(history, period, rates) : null
                      }
                      locale={locale}
                      rates={rates}
                    />
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </ResponsiveTable>
    </div>
  )
}
export function MacroDashboard({
  data,
  locale,
}: {
  data: MacroDashboardData | null
  locale: Locale
}) {
  const { t } = useTranslation()
  const [selectedFx, setSelectedFx] = useState("eur_usd")
  const [oilGoldDays, setOilGoldDays] = useState(365)
  const [copperGoldDays, setCopperGoldDays] = useState(365)
  const [dxyDays, setDxyDays] = useState(90)
  const [fxDays, setFxDays] = useState(90)
  const [compare, setCompare] = useState<Period>("week")
  const histories = data?.histories ?? []
  const byId = new Map(histories.map(item => [item.id, item]))
  const oilGold = ratioPoints(byId.get("wti"), byId.get("gold"))
  const copperGold = ratioPoints(byId.get("copper"), byId.get("gold"))
  const curve = yieldCurve(histories, compare)
  const line = (id: string, days: number): ChartLine => ({
    name: t(`macroAsset_${id}`),
    unit: byId.get(id)?.unit,
    points: recentPoints(byId.get(id), days).map(p => ({
      date: p.date,
      value: Number(p.value),
    })),
    change: byId.has(id) ? periodChange(byId.get(id)!, "day") : null,
  })
  function slicedRatio(
    points: { date: string; value: number }[],
    days: number
  ) {
    const latest = points.at(-1)
    return latest
      ? points.filter(
          p =>
            Date.parse(p.date) >=
            Date.parse(latest.date) - (days - 1) * 86_400_000
        )
      : []
  }
  return (
    <div className="min-w-0 pb-6 [&>section+section]:mt-9">
      <section className="min-w-0">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,560px),1fr))] items-start gap-6">
          <div className="col-span-full min-w-0">
            <DashboardPanel title={t("macroCommodities")}>
              <HistoryTable
                ids={commodityIds}
                histories={histories}
                locale={locale}
                fetchedAt={data?.fetched_at}
              />
            </DashboardPanel>
          </div>
          {/* One ratio per chart: a date missing from one pair must not break the other's line. */}
          <DashboardPanel
            title={t("macroOilGold")}
            controls={<Range value={oilGoldDays} onChange={setOilGoldDays} />}
          >
            <Chart
              locale={locale}
              label={t("macroOilGold")}
              days={oilGoldDays}
              lines={[
                {
                  name: t("macroOilGold"),
                  unit: "ratio",
                  points: slicedRatio(oilGold, oilGoldDays),
                },
              ]}
            />
          </DashboardPanel>
          <DashboardPanel
            title={t("macroCopperGold")}
            controls={
              <Range value={copperGoldDays} onChange={setCopperGoldDays} />
            }
          >
            <Chart
              locale={locale}
              label={t("macroCopperGold")}
              days={copperGoldDays}
              lines={[
                {
                  name: t("macroCopperGold"),
                  unit: "ratio",
                  points: slicedRatio(copperGold, copperGoldDays),
                },
              ]}
            />
          </DashboardPanel>
        </div>
      </section>
      <section className="min-w-0">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,560px),1fr))] items-start gap-6">
          <DashboardPanel title={t("macroYieldChanges")}>
            <HistoryTable
              ids={[...tenorIds, "sofr"]}
              histories={histories}
              locale={locale}
              rates
              fetchedAt={data?.fetched_at}
            />
          </DashboardPanel>
          <DashboardPanel
            title={t("macroYieldCurve")}
            controls={
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] text-sea-ink-soft">
                  {t("compareLabel")}
                </span>
                <DashboardChoices
                  label={t("compareLabel")}
                  value={compare}
                  onChange={setCompare}
                  options={periods.map(value => ({
                    value,
                    label: t(`macroPeriod_${value}`),
                  }))}
                />
              </div>
            }
          >
            <Chart
              locale={locale}
              label={t("macroYieldCurve")}
              curve
              lines={[
                {
                  name: t("curveNow"),
                  unit: "percent",
                  points: curve.points.map(p => ({
                    date: t(`macroAsset_${p.id}`),
                    value: p.value,
                  })),
                },
                {
                  name: t(`curveRef_${compare}`),
                  unit: "percent",
                  points: curve.points.map(p => ({
                    date: t(`macroAsset_${p.id}`),
                    value: p.reference,
                  })),
                },
              ]}
            />
          </DashboardPanel>
        </div>
      </section>
      <section className="min-w-0">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,560px),1fr))] items-start gap-6">
          <DashboardPanel
            title={t("macroDollarIndex")}
            controls={<Range value={dxyDays} onChange={setDxyDays} />}
          >
            <Chart
              locale={locale}
              label={t("macroDollarIndex")}
              lines={[line("dxy", dxyDays)]}
              days={dxyDays}
            />
          </DashboardPanel>
          <DashboardPanel
            title={t("macroFxTitle")}
            controls={<Range value={fxDays} onChange={setFxDays} />}
          >
            <div className="mt-3 font-mono">
              <DashboardChoices
                label={t("macroCurrencyPair")}
                value={selectedFx}
                onChange={setSelectedFx}
                options={fxIds.map(value => ({
                  value,
                  label: t(`macroAsset_${value}`),
                }))}
              />
            </div>
            <Chart
              locale={locale}
              label={`${t("macroFxTitle")} · ${t(`macroAsset_${selectedFx}`)}`}
              lines={[line(selectedFx, fxDays)]}
              days={fxDays}
              height={224}
            />
          </DashboardPanel>
        </div>
        <div className="mt-4">
          <DashboardPanel
            title={t("fxTableTitle")}
            controls={
              <span className="text-xs text-sea-ink-soft">
                {t("fxTableNote")}
              </span>
            }
          >
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,560px),1fr))] gap-6">
              {[fxIds.slice(0, 4), fxIds.slice(4)].map(ids => (
                <HistoryTable
                  key={ids[0]}
                  ids={ids}
                  histories={histories}
                  locale={locale}
                  selected={selectedFx}
                  onSelect={setSelectedFx}
                  fetchedAt={data?.fetched_at}
                />
              ))}
            </div>
          </DashboardPanel>
        </div>
      </section>
    </div>
  )
}
