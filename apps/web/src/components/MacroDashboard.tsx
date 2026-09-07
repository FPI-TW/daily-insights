import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useMemo, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { formatIsoDate, numberLocales, unitLabel } from "#/lib/format"
import {
  formatTaipeiTimestamp,
  periodChange,
  ratioPoints,
  recentPoints,
  type MacroDashboardData,
  type MacroHistory,
} from "#/lib/macro-dashboard"

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
const emptyHistories: MacroHistory[] = []

function Panel({
  title,
  note,
  children,
  wide = false,
}: {
  title: string
  note?: string
  children: ReactNode
  wide?: boolean
}) {
  return (
    <section
      className={`min-w-0 rounded-2xl border border-line bg-surface p-5 ${wide ? "xl:col-span-2" : ""}`}
    >
      <h2 className="m-0 text-base font-extrabold text-sea-ink">{title}</h2>
      {note ? (
        <p className="mt-1 mb-4 text-xs leading-5 text-sea-ink-soft">{note}</p>
      ) : null}
      {children}
    </section>
  )
}

export function MacroDashboardLoading() {
  const { t } = useTranslation()
  return (
    <div role="status" aria-live="polite" className="grid gap-4 xl:grid-cols-2">
      <span className="sr-only">{t("macroLoading")}</span>
      {[0, 1, 2, 3, 4, 5].map(key => (
        <div
          key={key}
          className="h-72 animate-pulse rounded-2xl border border-line bg-surface p-5 motion-reduce:animate-none"
        >
          <div className="mb-8 h-5 w-48 rounded bg-line" />
          <div className="h-44 rounded bg-line/60" />
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

type ChartLine = {
  name: string
  unit: string | undefined
  points: { date: string; value: number }[]
}

function fractionDigits(unit: string | undefined, value: number) {
  if (unit === "ratio") return 6
  if (unit === "percent" || unit === "index" || unit?.includes("/")) return 2
  if (unit && /^[A-Z]{3}$/.test(unit)) return Math.abs(value) < 10 ? 4 : 2
  return Math.abs(value) < 10 ? 4 : 2
}

function formatMacroNumber(
  value: number,
  unit: string | undefined,
  locale: Locale
) {
  return new Intl.NumberFormat(numberLocales[locale], {
    minimumFractionDigits: unit === "percent" ? 2 : 0,
    maximumFractionDigits: fractionDigits(unit, value),
  }).format(value)
}

function formatCalendarValue(
  value: string,
  unit: string | null,
  locale: Locale
) {
  const formatted = new Intl.NumberFormat(numberLocales[locale], {
    maximumFractionDigits: 4,
  }).format(Number(value))
  if (!unit) return formatted
  return unit === "%" ? `${formatted}%` : `${formatted} ${unit}`
}

function formatCountry(country: string, locale: Locale) {
  if (!/^[A-Z]{2}$/.test(country)) return country
  return (
    new Intl.DisplayNames(numberLocales[locale], { type: "region" }).of(
      country
    ) ?? country
  )
}
function Chart({
  lines,
  label,
  dual = false,
  categories,
  locale,
}: {
  lines: ChartLine[]
  label: string
  dual?: boolean
  categories?: string[]
  locale: Locale
}) {
  const colors = useChartColors()
  const { t } = useTranslation()
  if (!lines.some(line => line.points.length > 0)) return <Unavailable />
  const dates =
    categories ??
    [
      ...new Set(lines.flatMap(line => line.points.map(point => point.date))),
    ].sort()
  const valuesByLine = new Map(
    lines.map(line => [
      line.name,
      new Map(line.points.map(point => [point.date, point.value])),
    ])
  )
  const axes = lines.map((line, index) => ({
    type: "value",
    scale: true,
    name: dual
      ? `${line.name}${line.unit ? ` (${unitLabel(line.unit, t) ?? line.unit})` : ""}`
      : line.unit
        ? (unitLabel(line.unit, t) ?? line.unit)
        : "",
    position: index === 0 ? "left" : "right",
    nameTextStyle: { color: colors.text },
    axisLabel: { color: colors.text },
    splitLine: {
      show: index === 0,
      lineStyle: { color: colors.grid, type: "dashed" },
    },
  }))
  return (
    <>
      <div
        className="mt-4 flex flex-wrap gap-2"
        aria-label={t("macroLatestObservations")}
      >
        {lines.map(line => {
          const latest = line.points.at(-1)
          return latest ? (
            <div
              key={line.name}
              className="min-w-36 rounded-xl border border-line bg-line-soft/50 px-3 py-2"
            >
              <div className="text-[11px] text-sea-ink-soft">{line.name}</div>
              <div className="mt-0.5 flex items-baseline gap-1 tabular-nums">
                <strong className="font-mono text-sm text-sea-ink">
                  {formatMacroNumber(latest.value, line.unit, locale)}
                </strong>
                {line.unit ? (
                  <span className="text-[10px] text-sea-ink-soft">
                    {unitLabel(line.unit, t) ?? line.unit}
                  </span>
                ) : null}
              </div>
              <div className="mt-0.5 text-[10px] text-sea-ink-soft">
                {categories ? latest.date : formatIsoDate(latest.date, locale)}
              </div>
            </div>
          ) : null
        })}
      </div>
      <div className="mt-4" role="img" aria-label={label}>
        <ClientOnly
          fallback={<div className="h-64 animate-pulse rounded bg-line/40" />}
        >
          <ReactECharts
            style={{ height: 280 }}
            notMerge
            option={{
              animation: false,
              color: colors.series,
              tooltip: {
                trigger: "axis",
                renderMode: "richText",
                axisPointer: { type: "line" },
              },
              legend: { bottom: 0, textStyle: { color: colors.text } },
              grid: {
                left: 12,
                right: 12,
                top: 32,
                bottom: 40,
                containLabel: true,
              },
              xAxis: {
                type: "category",
                data: dates,
                boundaryGap: false,
                axisLabel: {
                  color: colors.text,
                  hideOverlap: true,
                  formatter: categories
                    ? undefined
                    : (day: string) =>
                        new Intl.DateTimeFormat(numberLocales[locale], {
                          month: "short",
                          day: "numeric",
                          timeZone: "UTC",
                        }).format(new Date(`${day}T00:00:00Z`)),
                },
                axisLine: { lineStyle: { color: colors.grid } },
              },
              yAxis: dual ? axes : axes[0],
              series: lines.map((line, index) => {
                const values = valuesByLine.get(line.name)!
                return {
                  name: line.name,
                  type: "line",
                  showSymbol: Boolean(categories),
                  connectNulls: false,
                  data: dates.map(day => values.get(day) ?? null),
                  lineStyle: { width: 2 },
                  emphasis: { focus: "series" },
                  tooltip: {
                    valueFormatter: (value: number) => {
                      const formatted = formatMacroNumber(
                        value,
                        line.unit,
                        locale
                      )
                      const unit = line.unit
                        ? (unitLabel(line.unit, t) ?? line.unit)
                        : ""
                      return unit ? `${formatted} ${unit}` : formatted
                    },
                  },
                  ...(dual ? { yAxisIndex: index } : {}),
                }
              }),
            }}
          />
        </ClientOnly>
      </div>
      <details className="mt-2 text-xs text-sea-ink-soft">
        <summary className="cursor-pointer">{t("macroChartData")}</summary>
        <div className="mt-2 max-h-56 overflow-auto">
          <table className="w-full text-right">
            <thead>
              <tr>
                <th className="text-left">{t("macroDate")}</th>
                {lines.map(line => (
                  <th key={line.name}>
                    {line.name}
                    {line.unit
                      ? ` (${unitLabel(line.unit, t) ?? line.unit})`
                      : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dates.map(day => (
                <tr key={day}>
                  <th className="text-left font-normal">
                    {categories ? day : formatIsoDate(day, locale)}
                  </th>
                  {lines.map(line => {
                    const value = valuesByLine.get(line.name)?.get(day)
                    return (
                      <td key={line.name}>
                        {value === undefined
                          ? "—"
                          : formatMacroNumber(value, line.unit, locale)}
                      </td>
                    )
                  })}
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
  fetchedAt: string | undefined
  ids: string[]
  histories: MacroHistory[]
  locale: Locale
  rates?: boolean
  selected?: string
  onSelect?: (id: string) => void
}) {
  const { t } = useTranslation()
  const format = (value: number, digits = 4) =>
    new Intl.NumberFormat(numberLocales[locale], {
      maximumFractionDigits: digits,
    }).format(value)
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[34rem] whitespace-nowrap text-right text-xs tabular-nums">
        <thead className="text-sea-ink-soft">
          <tr>
            <th className="py-3 text-left font-normal">
              {t(rates ? "macroTenor" : "macroInstrument")}
            </th>
            <th className="px-3 font-normal">
              {t(rates ? "macroYield" : "macroClose")}
            </th>
            <th className="px-3 font-normal">{t("macroDate")}</th>
            {["day", "week", "month", "year"].map(period => (
              <th key={period} className="px-3 font-normal">
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
                className={`border-t border-line ${selected === id ? "bg-lagoon/10" : ""}`}
              >
                <th className="py-3 text-left font-semibold text-sea-ink">
                  {onSelect ? (
                    <button
                      type="button"
                      aria-pressed={selected === id}
                      onClick={() => onSelect(id)}
                      className="cursor-pointer rounded px-2 py-1 text-left hover:bg-lagoon/10 focus-visible:outline-2 focus-visible:outline-lagoon"
                    >
                      {t(`macroAsset_${id}`)}
                    </button>
                  ) : (
                    t(`macroAsset_${id}`)
                  )}
                  {history ? (
                    <span className="mt-0.5 block font-mono text-[10px] font-normal text-sea-ink-soft">
                      {history.symbol} ·{" "}
                      {unitLabel(history.unit, t) ?? history.unit}
                    </span>
                  ) : null}
                </th>
                <td className="px-3 font-mono font-bold text-sea-ink">
                  {latest
                    ? `${formatMacroNumber(Number(latest.value), history?.unit, locale)}${rates ? "%" : ""}`
                    : "—"}
                </td>
                <td className="px-3 text-sea-ink-soft">
                  {latest
                    ? formatIsoDate(latest.date, locale)
                    : t("macroUnavailableShort")}
                  {stale ? (
                    <span className="ml-1 text-market-caution">
                      {t("reportStale")}
                    </span>
                  ) : null}
                </td>
                {(["day", "week", "month", "year"] as const).map(period => {
                  const value = history
                    ? periodChange(history, period, rates)
                    : null
                  return (
                    <td
                      key={period}
                      className={`px-3 font-mono ${value === null || Math.abs(value) < 0.005 ? "text-sea-ink-soft" : value > 0 ? "text-market-up" : "text-market-down"}`}
                    >
                      {value === null
                        ? "—"
                        : `${value > 0 ? "+" : ""}${format(value, 2)}${rates ? " bp" : "%"}`}
                    </td>
                  )
                })}
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
  const [days, setDays] = useState(90)
  const histories = data?.histories ?? emptyHistories
  const historyById = useMemo(
    () => new Map(histories.map(item => [item.id, item])),
    [histories]
  )
  const history = (id: string) => historyById.get(id)
  const line = (id: string, range = 90): ChartLine => {
    const selected = history(id)
    return {
      name: t(`macroAsset_${id}`),
      unit: selected?.unit,
      points: recentPoints(selected, range).map(point => ({
        ...point,
        value: Number(point.value),
      })),
    }
  }
  const curveHistories = tenorIds
    .map(history)
    .filter((item): item is MacroHistory => Boolean(item?.points.length))
  const commonDates =
    curveHistories.length === tenorIds.length
      ? curveHistories[0]!.points
          .map(point => point.date)
          .filter(day =>
            curveHistories.every(item =>
              item.points.some(point => point.date === day)
            )
          )
      : []
  const curveDate = commonDates.at(-1)
  const curve: ChartLine = {
    name: t("macroYield"),
    unit: "percent",
    points: curveDate
      ? curveHistories.map(item => ({
          date: t(`macroAsset_${item.id}`),
          value: Number(
            item.points.find(point => point.date === curveDate)!.value
          ),
        }))
      : [],
  }
  const calendar = data?.calendar
  const showCalendarImpact = Boolean(
    calendar?.events.some(event => event.impact)
  )
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-sea-ink-soft">
        <p className="m-0">{t("macroIntro")}</p>
        {data ? (
          <span>
            {t("macroFetched", {
              date: formatTaipeiTimestamp(data.fetched_at),
            })}
          </span>
        ) : null}
      </div>
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <Panel title={t("macroCommodities")} note={t("macroCommodityNote")}>
          <HistoryTable
            fetchedAt={data?.fetched_at}
            ids={commodityIds}
            histories={histories}
            locale={locale}
          />
        </Panel>
        <Panel
          title={t("reportBlockMacroCommodityRatios")}
          note={t("macroRatioNote")}
        >
          <Chart
            locale={locale}
            label={t("reportBlockMacroCommodityRatios")}
            dual
            lines={[
              {
                name: t("macroOilGold"),
                unit: "ratio",
                points: ratioPoints(history("wti"), history("gold")),
              },
              {
                name: t("macroCopperGold"),
                unit: "ratio",
                points: ratioPoints(history("copper"), history("gold")),
              },
            ]}
          />
        </Panel>
        <Panel title={t("macroYieldChanges")} note={t("macroYieldNote")}>
          <HistoryTable
            fetchedAt={data?.fetched_at}
            ids={[...tenorIds, "sofr"]}
            histories={histories}
            locale={locale}
            rates
          />
        </Panel>
        <Panel
          title={t("macroYieldCurve")}
          note={
            curveDate
              ? `${t("macroSourceTreasury")} · ${formatIsoDate(curveDate, locale)}`
              : t("macroSourceTreasury")
          }
        >
          <Chart
            locale={locale}
            label={t("macroYieldCurve")}
            lines={[curve]}
            categories={tenorIds.map(id => t(`macroAsset_${id}`))}
          />
        </Panel>
        <Panel
          title={t("macroCalendar")}
          note={t("macroCalendarNote", {
            source: calendar?.source ?? "Nasdaq",
            date: calendar ? formatIsoDate(calendar.date, locale) : "—",
          })}
        >
          {calendar?.status !== "ok" ? (
            <Unavailable />
          ) : calendar.events.length === 0 ? (
            <p className="py-10 text-sm text-sea-ink-soft">
              {t("macroNoEvents", { date: calendar.date })}
            </p>
          ) : (
            <div className="mt-4 max-h-96 overflow-auto rounded-xl border border-line">
              <table className="w-full min-w-[34rem] text-right text-xs tabular-nums">
                <thead className="text-sea-ink-soft">
                  <tr>
                    {[
                      "Time",
                      "Event",
                      ...(showCalendarImpact ? ["Impact"] : []),
                      "Estimate",
                      "Previous",
                      "Actual",
                    ].map(key => (
                      <th key={key} className="px-2 py-3 text-left font-normal">
                        {t(`macro${key}`)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {calendar.events.map((event, index) => (
                    <tr
                      key={`${event.date}-${event.event}-${index}`}
                      className="border-t border-line"
                    >
                      <td className="px-2 py-3 font-mono text-lagoon">
                        {new Date(event.date).toLocaleTimeString(
                          numberLocales[locale],
                          {
                            timeZone: "Asia/Taipei",
                            hour: "2-digit",
                            minute: "2-digit",
                            hour12: false,
                          }
                        )}
                      </td>
                      <th className="min-w-40 px-2 py-3 text-left font-normal">
                        <span className="mr-1 text-sea-ink-soft">
                          {formatCountry(event.country, locale)}
                        </span>
                        {event.currency ? (
                          <span className="mr-1 rounded bg-lagoon/10 px-1.5 py-0.5 font-mono text-[10px] font-bold text-lagoon">
                            {event.currency}
                          </span>
                        ) : null}
                        {event.event}
                      </th>
                      {showCalendarImpact ? (
                        <td className="px-2 text-market-caution">
                          {event.impact
                            ? t(`macroImpact_${event.impact.toLowerCase()}`, {
                                defaultValue: event.impact,
                              })
                            : "—"}
                        </td>
                      ) : null}
                      {[event.estimate, event.previous, event.actual].map(
                        (value, cell) => (
                          <td
                            key={cell}
                            className="whitespace-nowrap px-2 font-mono"
                          >
                            {value === null
                              ? cell === 2
                                ? t("macroUnreleased")
                                : "—"
                              : formatCalendarValue(value, event.unit, locale)}
                          </td>
                        )
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
        <Panel title={t("macroDollarIndex")} note={t("macroDxyNote")}>
          <Chart
            locale={locale}
            label={t("macroDollarIndex")}
            lines={[line("dxy")]}
          />
        </Panel>
        <Panel title={t("macroFxTitle")} note={t("macroFxNote")} wide>
          <div className="mt-4 flex flex-wrap items-end justify-between gap-3">
            <label className="flex items-center gap-2 text-sm text-sea-ink-soft">
              <span className="whitespace-nowrap">
                {t("macroCurrencyPair")}
              </span>
              <select
                value={selectedFx}
                onChange={event => setSelectedFx(event.target.value)}
                className="rounded-lg border border-line bg-surface px-3 py-2 text-sea-ink"
              >
                {fxIds.map(id => (
                  <option value={id} key={id}>
                    {t(`macroAsset_${id}`)}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex gap-1" aria-label={t("macroRange")}>
              {[30, 90, 365].map(range => (
                <button
                  type="button"
                  key={range}
                  aria-pressed={days === range}
                  onClick={() => setDays(range)}
                  className={`rounded-lg px-3 py-2 text-xs font-bold ${days === range ? "bg-lagoon/15 text-lagoon" : "text-sea-ink-soft hover:bg-lagoon/10"}`}
                >
                  {t("macroDays", { count: range })}
                </button>
              ))}
            </div>
          </div>
          <Chart
            locale={locale}
            label={`${t("macroFxTitle")} · ${t(`macroAsset_${selectedFx}`)}`}
            lines={[line(selectedFx, days)]}
          />
          <HistoryTable
            fetchedAt={data?.fetched_at}
            ids={fxIds}
            histories={histories}
            locale={locale}
            selected={selectedFx}
            onSelect={setSelectedFx}
          />
        </Panel>
      </div>
      <p className="text-xs leading-5 text-sea-ink-soft">
        {t("macroPeriodNote")}
      </p>
    </div>
  )
}
