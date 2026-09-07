import type {
  InstitutionalFlows,
  InstitutionalStocks,
  Locale,
} from "@daily-insights/api-client"
import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { useChartColors } from "#/lib/chart"
import { numberLocales } from "#/lib/format"
import type { TaiwanInstitutionalData } from "#/lib/institutional-flows"
import type { MarketIndexHistory } from "#/lib/indices"
import {
  DashboardChoices,
  DashboardPanel,
  DashboardSection,
  Methodology,
} from "./DashboardPrimitives"

type Institution = "all" | "foreign" | "trust" | "dealer"
type FlowMode = "daily" | "cum"
type FlowRange = 20 | 40 | 60

function unavailableText() {
  return "indexChartUnavailable"
}

function signed(value: number, locale: Locale, digits: number) {
  return new Intl.NumberFormat(numberLocales[locale], {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
    signDisplay: "exceptZero",
  }).format(Math.abs(value) < 0.05 ? 0 : value)
}

function niceStep(value: number) {
  if (!Number.isFinite(value) || value <= 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(value))
  const normalized = value / magnitude
  const factor =
    normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return factor * magnitude
}

function directionClass(value: number) {
  if (Math.abs(value) < 0.05) return "text-sea-ink-soft"
  return value > 0 ? "text-market-up" : "text-market-down"
}

function FlowPanel({
  flows,
  history,
  locale,
}: {
  flows: InstitutionalFlows | null
  history: MarketIndexHistory | null
  locale: Locale
}) {
  const { t } = useTranslation()
  const colors = useChartColors()
  const [institution, setInstitution] = useState<Institution>("all")
  const [mode, setMode] = useState<FlowMode>("daily")
  const [range, setRange] = useState<FlowRange>(40)
  const visible = useMemo(
    () => flows?.series.slice(-range) ?? [],
    [flows, range]
  )
  const dailyValues = visible.map(point =>
    Number(point[institution === "all" ? "total" : institution])
  )
  let running = 0
  const values = dailyValues.map(value => {
    running += value
    return mode === "cum" ? running : value
  })
  const dates = visible.map(point => point.trade_date)
  const indexBars =
    history?.series.find(item => item.symbol === "^TWII")?.bars ?? []
  const indexByDate = new Map(
    indexBars.map(bar => [bar.trade_date, Number(bar.close)])
  )
  const indexValues = dates.map(day => indexByDate.get(day) ?? null)
  const indexNumbers = indexValues.flatMap(value =>
    value === null ? [] : [value]
  )
  const maxAbs = Math.max(0, ...values.map(Math.abs))
  const leftInterval = niceStep(maxAbs / 3)
  const leftMax = leftInterval * 3
  const indexMin = indexNumbers.length ? Math.min(...indexNumbers) : 0
  const indexMax = indexNumbers.length ? Math.max(...indexNumbers) : 1
  const indexPadding =
    indexMax === indexMin
      ? Math.max(1, indexMax * 0.01)
      : (indexMax - indexMin) * 0.08
  const rightMin = indexMin - indexPadding
  const rightMax = indexMax + indexPadding
  const latest = dailyValues.at(-1) ?? 0
  const sum = dailyValues.reduce((total, value) => total + value, 0)
  const ready = visible.length > 0 && indexNumbers.length > 0

  return (
    <DashboardPanel
      title={t("flowTitle")}
      controls={
        <DashboardChoices
          label={t("flowTitle")}
          value={mode}
          onChange={setMode}
          options={(["daily", "cum"] as const).map(value => ({
            value,
            label: t(`flowMode_${value}`),
          }))}
        />
      }
    >
      <p className="mt-1 text-xs text-sea-ink-soft">{t("flowSub")}</p>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <DashboardChoices
          label={t("sectionFlows")}
          value={institution}
          onChange={setInstitution}
          options={(["all", "foreign", "trust", "dealer"] as const).map(
            value => ({
              value,
              label: t(`inst_${value}`),
            })
          )}
        />
        <DashboardChoices
          label={t("flowRangeLabel")}
          value={range}
          onChange={setRange}
          options={([20, 40, 60] as const).map(value => ({
            value,
            label: t(`flowRange_${value}`),
          }))}
        />
      </div>
      {!ready ? (
        <p
          role="status"
          className="flex min-h-64 items-center justify-center text-sm text-sea-ink-soft"
        >
          {t(unavailableText())}
        </p>
      ) : (
        <>
          <div className="mt-3.5 flex flex-wrap items-baseline gap-[18px]">
            <span className="text-xs text-sea-ink-soft">{t("flowLatest")}</span>
            <strong
              className={`font-mono text-[17px] tabular-nums ${directionClass(latest)}`}
            >
              {signed(latest, locale, 1)}{" "}
              <small className="text-[11px]">{t("flowUnitNet")}</small>
            </strong>
            <span className="text-xs text-sea-ink-soft">{t("flowSum")}</span>
            <strong
              className={`font-mono text-[13px] tabular-nums ${directionClass(sum)}`}
            >
              {signed(sum, locale, 1)} {t("flowUnitNet")}
            </strong>
          </div>
          <ClientOnly
            fallback={
              <div className="mt-3 h-64 animate-pulse rounded bg-line/60" />
            }
          >
            <ReactECharts
              style={{ height: 276 }}
              notMerge
              option={{
                animation: false,
                grid: { left: 48, right: 56, top: 14, bottom: 58 },
                tooltip: {
                  trigger: "axis",
                  formatter: (params: Array<{ dataIndex: number }>) => {
                    const index = params[0]?.dataIndex ?? 0
                    const indexValue = indexValues[index]
                    return [
                      dates[index],
                      `${t("flowTitle")}: ${signed(values[index] ?? 0, locale, 1)} ${t("flowUnitNet")}`,
                      `${t("flowLegendIndex")}: ${indexValue == null ? "—" : new Intl.NumberFormat(numberLocales[locale], { maximumFractionDigits: 0 }).format(indexValue)}`,
                    ].join("<br/>")
                  },
                },
                legend: {
                  bottom: 0,
                  data: [
                    t("flowLegendBuy"),
                    t("flowLegendSell"),
                    t("flowLegendIndex"),
                  ],
                  itemWidth: 10,
                  itemHeight: 10,
                  itemGap: 20,
                  textStyle: { color: colors.text, fontSize: 11 },
                },
                xAxis: {
                  type: "category",
                  data: dates,
                  axisTick: { show: false },
                  axisLine: { lineStyle: { color: colors.grid } },
                  axisLabel: {
                    color: colors.text,
                    fontFamily: "monospace",
                    fontSize: 10,
                    interval: Math.max(0, Math.ceil(dates.length / 7) - 1),
                    formatter: (value: string) => value.slice(5),
                  },
                },
                yAxis: [
                  {
                    type: "value",
                    min: -leftMax,
                    max: leftMax,
                    interval: leftInterval,
                    splitNumber: 6,
                    axisLabel: {
                      color: colors.text,
                      fontFamily: "monospace",
                      fontSize: 10,
                    },
                    splitLine: {
                      lineStyle: { color: colors.gridSoft, type: "dashed" },
                    },
                  },
                  {
                    type: "value",
                    min: rightMin,
                    max: rightMax,
                    interval: (rightMax - rightMin) / 6,
                    splitNumber: 6,
                    axisLabel: {
                      color: colors.text,
                      fontFamily: "monospace",
                      fontSize: 10,
                      formatter: (value: number) =>
                        Math.round(value).toLocaleString(numberLocales[locale]),
                    },
                    splitLine: { show: false },
                  },
                ],
                series: [
                  {
                    name: t("flowLegendBuy"),
                    type: "bar",
                    data: values.map(value => (value >= 0 ? value : null)),
                    barWidth: "58%",
                    itemStyle: { color: colors.up, opacity: 0.75 },
                    markLine: {
                      silent: true,
                      symbol: "none",
                      label: { show: false },
                      lineStyle: {
                        color: colors.chipLine,
                        type: "solid",
                        width: 1,
                      },
                      data: [{ yAxis: 0 }],
                    },
                  },
                  {
                    name: t("flowLegendSell"),
                    type: "bar",
                    data: values.map(value => (value < 0 ? value : null)),
                    barWidth: "58%",
                    barGap: "-100%",
                    itemStyle: { color: colors.down, opacity: 0.75 },
                  },
                  {
                    name: t("flowLegendIndex"),
                    type: "line",
                    yAxisIndex: 1,
                    data: indexValues,
                    symbol: "none",
                    connectNulls: false,
                    lineStyle: { color: colors.indexSeries[0], width: 1.6 },
                    itemStyle: { color: colors.indexSeries[0] },
                  },
                ],
              }}
            />
          </ClientOnly>
          <Methodology>{t("flowFormula")}</Methodology>
        </>
      )}
    </DashboardPanel>
  )
}

function FlowTable({
  title,
  rows,
  locale,
  direction,
}: {
  title: string
  rows: InstitutionalStocks["rows"]
  locale: Locale
  direction: "up" | "down"
}) {
  const { t } = useTranslation()
  return (
    <div className={direction === "down" ? "mt-6" : ""}>
      <h4 className="mb-3 flex items-center gap-2 text-sm font-bold">
        <span
          className={
            direction === "up"
              ? "size-2 rounded-xs bg-market-up"
              : "size-2 rounded-xs bg-market-down"
          }
        />
        {title}
      </h4>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full min-w-160 table-auto text-sm leading-6">
          <colgroup>
            <col className="w-44" />
            <col />
            <col />
            <col />
            <col className="w-32" />
          </colgroup>
          <thead className="text-sea-ink-soft">
            <tr>
              {[
                "colStock",
                "colForeign",
                "colTrust",
                "colDealer",
                "colTotal",
              ].map((key, index) => (
                <th
                  key={key}
                  className={`border-b border-line whitespace-nowrap px-4 py-3 font-semibold ${index === 0 ? "text-left" : "text-right"}`}
                >
                  {t(key)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr
                key={row.symbol}
                className="border-t border-line-soft first:border-t-0"
              >
                <td className="px-4 py-4 font-semibold text-sea-ink">
                  <span className="block whitespace-nowrap">{row.name}</span>
                  <span className="mt-1 block font-mono text-xs text-sea-ink-soft">
                    {row.symbol}
                  </span>
                </td>
                {[
                  row.foreign_lots,
                  row.trust_lots,
                  row.dealer_lots,
                  row.total_lots,
                ].map((raw, index) => {
                  const value = Number(raw)
                  return (
                    <td
                      key={index}
                      className={`whitespace-nowrap px-4 py-4 text-right font-mono tabular-nums ${index === 3 ? "font-bold" : ""} ${directionClass(value)}`}
                    >
                      {signed(value, locale, value % 1 === 0 ? 0 : 1)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StocksPanel({
  stocks,
  locale,
}: {
  stocks: InstitutionalStocks | null
  locale: Locale
}) {
  const { t } = useTranslation()
  const buys = [...(stocks?.rows ?? [])]
    .filter(row => Number(row.total_lots) > 0)
    .sort((a, b) => Number(b.total_lots) - Number(a.total_lots))
    .slice(0, 5)
  const sells = [...(stocks?.rows ?? [])]
    .filter(row => Number(row.total_lots) < 0)
    .sort((a, b) => Number(a.total_lots) - Number(b.total_lots))
    .slice(0, 5)
  return (
    <DashboardPanel
      title={t("stockTitle")}
      controls={
        <span className="rounded-full border border-line bg-chip px-2.5 py-[3px] text-[11px] font-bold text-sea-ink-soft">
          {t("listedOnly")}
        </span>
      }
    >
      <p className="mt-1 text-xs text-sea-ink-soft">{t("stockSub")}</p>
      {!stocks || (!buys.length && !sells.length) ? (
        <p
          role="status"
          className="flex min-h-64 items-center justify-center text-sm text-sea-ink-soft"
        >
          {t(unavailableText())}
        </p>
      ) : (
        <>
          <div className="mt-4">
            <FlowTable
              title={t("topBuy5")}
              rows={buys}
              locale={locale}
              direction="up"
            />
            <FlowTable
              title={t("topSell5")}
              rows={sells}
              locale={locale}
              direction="down"
            />
          </div>
          <p className="mt-4 text-xs leading-5 text-pretty text-sea-ink-soft">
            {t("stockNote", { date: stocks.as_of ?? "—" })}
          </p>
        </>
      )}
    </DashboardPanel>
  )
}

export function TaiwanInstitutionalFlows({
  data,
  history,
  locale,
}: {
  data: TaiwanInstitutionalData
  history: MarketIndexHistory | null
  locale: Locale
}) {
  const { t } = useTranslation()
  const asOf = data.flows?.as_of ?? data.stocks?.as_of ?? "—"
  return (
    <div className="mt-6 min-w-0">
      <DashboardSection
        number="02"
        title={t("sectionFlows")}
        meta={t("flowsMeta", { date: asOf })}
      >
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,440px),1fr))] items-start gap-6">
          <FlowPanel flows={data.flows} history={history} locale={locale} />
          <StocksPanel stocks={data.stocks} locale={locale} />
        </div>
      </DashboardSection>
    </div>
  )
}

export function TaiwanInstitutionalFlowsLoading() {
  const { t } = useTranslation()
  return (
    <section
      role="status"
      aria-live="polite"
      aria-label={t("flowLoading")}
      className="mt-6 min-w-0"
    >
      <div className="mb-3 h-5 w-32 animate-pulse rounded bg-line" />
      <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,440px),1fr))] gap-6">
        {[0, 1].map(panel => (
          <div
            key={panel}
            className="rounded-2xl border border-line bg-surface p-5"
          >
            <div className="h-5 w-48 animate-pulse rounded bg-line" />
            <div className="mt-4 h-64 animate-pulse rounded bg-line/60" />
          </div>
        ))}
      </div>
    </section>
  )
}
