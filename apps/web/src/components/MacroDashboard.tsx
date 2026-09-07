import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { numberLocales, unitLabel } from "#/lib/format"
import {
  alignedRatioAxes,
  formatTaipeiTimestamp,
  periodChange,
  ratioPoints,
  recentPoints,
  yieldCurve,
  type MacroDashboardData,
  type MacroHistory,
  type Period,
} from "#/lib/macro-dashboard"
import {
  DashboardChoices,
  DashboardPanel,
  Methodology,
} from "./DashboardPrimitives"

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
          className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,440px),1fr))] gap-4"
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
  compact = false,
}: {
  value: number | null
  locale: Locale
  rates?: boolean
  compact?: boolean
}) {
  const flat = value !== null && Math.abs(value) < 0.005
  return (
    <span
      className={`whitespace-nowrap font-mono font-bold tabular-nums ${compact ? "text-[11px] @min-[520px]:text-xs" : "text-xs"} ${value === null || flat ? "text-sea-ink-soft" : value > 0 ? "text-market-up" : "text-market-down"}`}
    >
      {value === null
        ? "—"
        : `${flat ? "" : value > 0 ? "▲" : "▼"}${new Intl.NumberFormat(numberLocales[locale], { maximumFractionDigits: 2 }).format(flat ? 0 : Math.abs(value))}${rates ? "" : "%"}`}
      {value !== null && rates ? (
        <span className="font-sans text-[10px]"> bp</span>
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
              className="flex flex-wrap items-baseline gap-1.5 text-xs text-sea-ink-soft"
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
      <details className="mt-2 text-xs text-sea-ink-soft">
        <summary className="cursor-pointer hover:text-palm">
          {t("macroChartData")}
        </summary>
        <div className="mt-2 max-h-56 overflow-auto">
          <table className="w-full table-fixed text-right font-mono tabular-nums">
            <thead>
              <tr>
                <th className="text-left">{t("macroDate")}</th>
                {lines.map(line => (
                  <th key={line.name}>{line.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dates.map(date => (
                <tr key={date}>
                  <th className="text-left font-normal">{date}</th>
                  {lines.map((line, i) => (
                    <td key={line.name}>
                      {formatValue(maps[i]!.get(date), line.unit, locale)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
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
  if (
    !ids.some(id =>
      histories.some(history => history.id === id && history.points.length)
    )
  )
    return <Unavailable />
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full table-fixed text-right text-xs tabular-nums">
        <colgroup>
          <col
            className={
              fx
                ? "w-[24%]"
                : rates
                  ? "w-[12%] @min-[520px]:w-[22%]"
                  : "w-[23%] @min-[520px]:w-[28%]"
            }
          />
          <col
            className={
              fx
                ? "w-[18%]"
                : rates
                  ? "w-[12%] @min-[520px]:w-[16%]"
                  : "w-[15%] @min-[520px]:w-[17%]"
            }
          />
          {!fx ? <col className="w-[18%] @min-[520px]:w-[15%]" /> : null}
          {periods.map(period => (
            <col key={period} />
          ))}
        </colgroup>
        <thead className="border-b border-line text-sea-ink-soft">
          <tr>
            <th className="pb-2 text-left font-semibold">
              {t(rates ? "macroTenor" : "macroInstrument")}
            </th>
            <th className="pb-2 font-semibold">
              {t(rates ? "macroYield" : "macroClose")}
            </th>
            {!fx ? (
              <th className="pb-2 font-semibold">{t("macroDate")}</th>
            ) : null}
            {periods.map(period => (
              <th key={period} className="pb-2 font-semibold">
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
                <th className="py-2.5 pr-1 text-left font-semibold text-pretty text-sea-ink">
                  {onSelect ? (
                    <button
                      type="button"
                      aria-pressed={selected === id}
                      onClick={() => onSelect(id)}
                      className={`border-0 bg-transparent p-0 text-left font-mono text-xs font-bold underline-offset-3 hover:underline hover:decoration-lagoon ${selected === id ? "text-palm" : "text-sea-ink"}`}
                    >
                      {t(`macroAsset_${id}`)}
                    </button>
                  ) : (
                    t(`macroAsset_${id}`)
                  )}
                  {history && !fx ? (
                    <span className="mt-0.5 block font-mono text-[10px] font-normal text-sea-ink-soft">
                      {history.symbol} ·{" "}
                      {unitLabel(history.unit, t) ?? history.unit}
                    </span>
                  ) : null}
                  {stale ? (
                    <span className="block text-[10px] font-normal text-sea-ink-soft">
                      {t("reportStale")}
                    </span>
                  ) : null}
                </th>
                <td className="font-mono text-[13px] font-bold text-sea-ink">
                  {formatValue(
                    latest ? Number(latest.value) : null,
                    history?.unit,
                    locale
                  )}
                </td>
                {!fx ? (
                  <td className="font-mono text-[11px] text-sea-ink-soft">
                    {latest?.date ?? "—"}
                  </td>
                ) : null}
                {periods.map(period => (
                  <td key={period}>
                    <Change
                      value={
                        history ? periodChange(history, period, rates) : null
                      }
                      locale={locale}
                      rates={rates}
                      compact
                    />
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
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
  const [ratioDays, setRatioDays] = useState(365)
  const [dxyDays, setDxyDays] = useState(90)
  const [fxDays, setFxDays] = useState(90)
  const [compare, setCompare] = useState<Period>("week")
  const histories = data?.histories ?? []
  const byId = new Map(histories.map(item => [item.id, item]))
  const oilGold = ratioPoints(byId.get("wti"), byId.get("gold"))
  const copperGold = ratioPoints(byId.get("copper"), byId.get("gold"))
  const curve = yieldCurve(histories, compare)
  const fxDates = [
    ...new Set(fxIds.flatMap(id => byId.get(id)?.points.at(-1)?.date ?? [])),
  ].sort()
  const allFxSameDate =
    fxDates.length === 1 && fxIds.every(id => byId.get(id)?.points.length)
  const line = (id: string, days: number): ChartLine => ({
    name: t(`macroAsset_${id}`),
    unit: byId.get(id)?.unit,
    points: recentPoints(byId.get(id), days).map(p => ({
      date: p.date,
      value: Number(p.value),
    })),
    change: byId.has(id) ? periodChange(byId.get(id)!, "day") : null,
  })
  function slicedRatio(points: { date: string; value: number }[]) {
    const latest = points.at(-1)
    return latest
      ? points.filter(
          p =>
            Date.parse(p.date) >=
            Date.parse(latest.date) - (ratioDays - 1) * 86_400_000
        )
      : []
  }
  return (
    <div className="min-w-0 pb-6 [&>section+section]:mt-9">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3 text-xs text-sea-ink-soft">
        <div className="flex flex-wrap items-center gap-3">
          <p className="m-0 max-w-[52ch] text-pretty">{t("macroIntro")}</p>
          <span className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-2.5 py-0.5 font-bold">
            <span className="text-market-up">▲ {t("convUp")}</span>
            <span className="h-2.5 w-px bg-line" />
            <span className="text-market-down">▼ {t("convDown")}</span>
          </span>
        </div>
        {data ? (
          <span className="whitespace-nowrap font-mono tabular-nums">
            {t("macroFetched", {
              date: formatTaipeiTimestamp(data.fetched_at),
            })}
          </span>
        ) : null}
      </div>
      <section className="min-w-0">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,440px),1fr))] items-start gap-4">
          <DashboardPanel title={t("macroCommodities")}>
            <HistoryTable
              ids={commodityIds}
              histories={histories}
              locale={locale}
              fetchedAt={data?.fetched_at}
            />
            <Methodology>{t("macroCommodityNote")}</Methodology>
          </DashboardPanel>
          <DashboardPanel
            title={t("reportBlockMacroCommodityRatios")}
            controls={<Range value={ratioDays} onChange={setRatioDays} />}
          >
            <Chart
              locale={locale}
              label={t("reportBlockMacroCommodityRatios")}
              dual
              days={ratioDays}
              lines={[
                {
                  name: t("macroOilGold"),
                  unit: "ratio",
                  points: slicedRatio(oilGold),
                },
                {
                  name: t("macroCopperGold"),
                  unit: "ratio",
                  points: slicedRatio(copperGold),
                },
              ]}
            />
            <Methodology>{t("macroRatioNote")}</Methodology>
          </DashboardPanel>
        </div>
      </section>
      <section className="min-w-0">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,440px),1fr))] items-start gap-4">
          <DashboardPanel title={t("macroYieldChanges")}>
            <HistoryTable
              ids={[...tenorIds, "sofr"]}
              histories={histories}
              locale={locale}
              rates
              fetchedAt={data?.fetched_at}
            />
            <Methodology>{t("macroYieldNote")}</Methodology>
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
            <p className="mt-2 mb-0 font-mono text-xs text-sea-ink-soft">
              {curve.date ?? "—"}
            </p>
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
            <Methodology>{t("curveNote")}</Methodology>
          </DashboardPanel>
        </div>
      </section>
      <section className="min-w-0">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,440px),1fr))] items-start gap-4">
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
            <Methodology>{t("macroDxyNote")}</Methodology>
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
            <Methodology>{t("macroFxNote")}</Methodology>
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
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,360px),1fr))] gap-x-8">
              {[fxIds.slice(0, 4), fxIds.slice(4)].map((ids, i) => (
                <HistoryTable
                  key={i}
                  ids={ids}
                  histories={histories}
                  locale={locale}
                  selected={selectedFx}
                  onSelect={setSelectedFx}
                  fetchedAt={data?.fetched_at}
                />
              ))}
            </div>
            <p className="mt-3 mb-0 font-mono text-xs text-sea-ink-soft tabular-nums">
              {allFxSameDate
                ? t("fxAsOf", { date: fxDates[0] })
                : fxIds
                    .map(
                      id =>
                        `${t(`macroAsset_${id}`)} ${byId.get(id)?.points.at(-1)?.date ?? "—"}`
                    )
                    .join(" · ")}
            </p>
          </DashboardPanel>
        </div>
      </section>
      <p className="mt-5 max-w-[100ch] text-xs leading-5 text-pretty text-sea-ink-soft">
        {t("macroPeriodNote")}
      </p>
    </div>
  )
}
