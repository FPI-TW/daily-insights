import { ClientOnly } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { useTranslation } from "react-i18next"
import type { Locale } from "@daily-insights/api-client"
import { useChartColors } from "#/lib/chart"
import { formatIsoDate, formatNumber } from "#/lib/format"
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
  const values = history.bars.map(bar => Number(bar.close))
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const chartMinimum = Math.max(0, Math.min(15, Math.floor(minimum - 2)))
  const chartMaximum = Math.max(35, Math.ceil(maximum + 2))
  const closeLabel = t("vixChartClose")

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
      <div className="h-84 min-w-0 w-full overflow-hidden border-y border-line py-2 sm:h-96">
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
              color: [colors.indexSeries[0]],
              grid: { left: 64, right: 18, top: 36, bottom: 72 },
              tooltip: {
                trigger: "axis",
                appendToBody: true,
                valueFormatter: (value: number | string) =>
                  formatNumber(value, "index", locale),
              },
              xAxis: {
                type: "category",
                data: history.bars.map(bar => bar.trade_date),
                boundaryGap: false,
                axisLabel: { color: colors.text },
                axisLine: { lineStyle: { color: colors.grid } },
              },
              yAxis: {
                type: "value",
                min: chartMinimum,
                max: chartMaximum,
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
                  name: closeLabel,
                  type: "line",
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
              ],
            }}
          />
        </ClientOnly>
      </div>
      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs text-sea-ink-soft">
        <div className="flex gap-1">
          <dt>{t("vixChartLatestDate")}</dt>
          <dd className="m-0 font-semibold text-sea-ink">
            {formatIsoDate(latest.trade_date, locale)}
          </dd>
        </div>
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
            </tr>
          </thead>
          <tbody>
            {history.bars.map(bar => (
              <tr key={bar.trade_date}>
                <th>{formatIsoDate(bar.trade_date, locale)}</th>
                <td>{formatNumber(bar.close, "index", locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
