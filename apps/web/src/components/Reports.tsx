import { ClientOnly, Link, useRouter } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { motion } from "motion/react"
import { useEffect, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import type { AnalystViewpoint, Locale } from "@daily-insights/api-client"
import {
  type MarketCode,
  navMarketCodes,
  type ProvisionalReport,
  type ReportBlock,
  type ReportValue,
} from "#/lib/provisional-reports"
import { fadeIn, reveal, useEnterAnimation } from "#/lib/motion"
import { ActiveIndicator } from "./ActiveIndicator"

function valueText(value: ReportValue | null, t: (key: string) => string) {
  if (value === null) return "—"
  return value.kind === "translation" ? t(value.key) : String(value.value)
}

function directionClass(value: ReportValue | null, t: (key: string) => string) {
  const text = valueText(value, t)
  if (text.startsWith("+")) return "text-market-up"
  if (text.startsWith("-")) return "text-market-down"
  return "text-sea-ink"
}

export function ReportLoadingScreen() {
  const { t } = useTranslation()
  const animate = useEnterAnimation()
  return (
    <div role="status" aria-live="polite">
      <p className="sr-only">{t("reportLoadingAnnouncement")}</p>
      <motion.div
        className="grid animate-pulse gap-4 md:grid-cols-2 xl:grid-cols-3"
        variants={fadeIn}
        initial={animate ? "hidden" : false}
        animate="visible"
      >
        <div className="h-44 rounded-[13px] bg-line" />
        <div className="h-44 rounded-[13px] bg-line" />
        <div className="h-44 rounded-[13px] bg-line" />
      </motion.div>
    </div>
  )
}

function ReportMarketNav({
  locale,
  activeMarket,
}: {
  locale: Locale
  activeMarket?: MarketCode | undefined
}) {
  const { t } = useTranslation()
  const linkClass = (active: boolean) =>
    `shrink-0 border-b-2 border-transparent px-4 py-3 text-xs font-extrabold no-underline transition-colors ${active ? "text-lagoon" : "text-sea-ink-soft hover:text-sea-ink"}`
  return (
    <nav
      className="relative isolate mb-6 flex overflow-x-auto border-y border-line bg-surface scrollbar-none [&::-webkit-scrollbar]:hidden [view-transition-name:report-market-nav]"
      aria-label={t("reportMarketNav")}
    >
      <ActiveIndicator
        activeKey={`${locale}:${activeMarket ?? "all"}`}
        variant="underline"
      />
      <Link
        to="/$locale/reports"
        params={{ locale }}
        activeOptions={{ exact: true }}
        className={linkClass(activeMarket === undefined)}
      >
        {t("reportAllMarkets")}
      </Link>
      {navMarketCodes.map(code => (
        <Link
          key={code}
          to="/$locale/reports/$marketCode"
          params={{ locale, marketCode: code }}
          className={linkClass(activeMarket === code)}
        >
          {t(`reportMarketShort_${code}`)}
        </Link>
      ))}
    </nav>
  )
}

function PageHeading({ title }: { title: string }) {
  return (
    <header className="mb-6">
      <h1 className="m-0 text-[30px] leading-tight font-extrabold tracking-[-0.035em] text-sea-ink max-sm:text-[26px]">
        {title}
      </h1>
      <div className="mt-3 h-0.75 w-13.5 bg-lagoon" />
    </header>
  )
}

export function ReportShell({
  locale,
  activeMarket,
  children,
}: {
  locale: Locale
  activeMarket?: MarketCode | undefined
  children: ReactNode
}) {
  const { t } = useTranslation()
  return (
    <main className="page-shell">
      <PageHeading
        title={
          activeMarket ? t(`reportMarket_${activeMarket}`) : t("reportsTitle")
        }
      />
      <ReportMarketNav locale={locale} activeMarket={activeMarket} />
      {children}
    </main>
  )
}

export function ReportList({
  locale: _locale,
  viewpoints = [],
}: {
  locale?: Locale
  viewpoints?: ReadonlyArray<AnalystViewpoint>
}) {
  return <AnalystViewpoints viewpoints={viewpoints} />
}

export function AnalystViewpointsLoading() {
  const { t } = useTranslation()
  return (
    <section
      className="mb-6 animate-pulse rounded-[13px] border border-line bg-surface p-5"
      aria-live="polite"
      aria-label={t("analystViewpointsLoading")}
    >
      <p className="sr-only">{t("analystViewpointsLoading")}</p>
      <div className="h-4 w-40 rounded bg-line" />
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {navMarketCodes.map(marketCode => (
          <div className="h-20 rounded bg-line" key={marketCode} />
        ))}
      </div>
    </section>
  )
}

function AnalystViewpoints({
  viewpoints,
}: {
  viewpoints: ReadonlyArray<AnalystViewpoint>
}) {
  const { t } = useTranslation()
  const viewpointsByMarket = new Map(
    viewpoints.map(viewpoint => [viewpoint.market_code, viewpoint])
  )
  const visibleViewpoints = navMarketCodes.flatMap(marketCode => {
    const viewpoint = viewpointsByMarket.get(marketCode)
    return viewpoint ? [viewpoint] : []
  })
  const latest = visibleViewpoints[0]
  if (!latest) return null
  return (
    <section className="mb-6" aria-labelledby="analyst-viewpoints-title">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2
          id="analyst-viewpoints-title"
          className="m-0 text-lg font-extrabold tracking-[-0.02em] text-sea-ink"
        >
          {t("analystViewpointsTitle")}
        </h2>
        <p className="m-0 text-xs text-sea-ink-soft">
          {t("analystViewpointsUpdated", {
            timestamp: new Intl.DateTimeFormat(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            }).format(new Date(latest.fetched_at)),
          })}
        </p>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {visibleViewpoints.map(viewpoint => (
          <article
            key={viewpoint.market_code}
            className="surface-panel border-t-[3px] border-t-lagoon p-4"
          >
            <h3 className="m-0 text-sm font-extrabold text-sea-ink">
              {t(`reportMarket_${viewpoint.market_code}`)}
            </h3>
            <ul className="mt-3 mb-0 grid list-disc gap-2 pl-5 text-sm leading-6 text-sea-ink-soft">
              {viewpoint.points.map(point => (
                <li key={point}>{point}</li>
              ))}
            </ul>
          </article>
        ))}
      </div>
    </section>
  )
}

export function ReportDetail({
  locale: _locale,
  report,
}: {
  locale?: Locale
  report: ProvisionalReport
}) {
  return (
    <div className="grid min-w-0 gap-4 xl:grid-cols-2">
      {report.blocks.map((block, index) => (
        <ReportBlockView
          block={block}
          index={index}
          key={`${block.titleKey}-${index}`}
        />
      ))}
    </div>
  )
}

function useChartColors() {
  const [colors, setColors] = useState({
    series: [] as string[],
    text: "",
    grid: "",
  })
  useEffect(() => {
    const updateColors = () => {
      const styles = getComputedStyle(document.documentElement)
      setColors({
        series: [
          styles.getPropertyValue("--lagoon-deep").trim(),
          styles.getPropertyValue("--lagoon").trim(),
          styles.getPropertyValue("--market-up").trim(),
          styles.getPropertyValue("--market-down").trim(),
          styles.getPropertyValue("--market-caution").trim(),
        ],
        text: styles.getPropertyValue("--sea-ink-soft").trim(),
        grid: styles.getPropertyValue("--line").trim(),
      })
    }
    updateColors()
    const observer = new MutationObserver(updateColors)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    })
    return () => observer.disconnect()
  }, [])
  return colors
}

function chartCategories(
  block: Extract<ReportBlock, { kind: "series" }>,
  t: (key: string) => string
) {
  const categories = Array.from(
    new Set(
      block.series.flatMap(line =>
        line.points.map(point => valueText(point.label, t))
      )
    )
  )
  return categories.length > 0 && categories.every(isIsoDateLabel)
    ? [...categories].sort()
    : categories
}

function chartDataForCategories(
  line: Extract<ReportBlock, { kind: "series" }>["series"][number],
  categories: ReadonlyArray<string>,
  t: (key: string) => string
) {
  const valuesByCategory = new Map(
    line.points.map(point => [valueText(point.label, t), point.value])
  )
  return categories.map(category => valuesByCategory.get(category) ?? null)
}

function isIsoDateLabel(label: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label)
  if (!match) return false

  const [, year, month, day] = match
  return (
    new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)))
      .toISOString()
      .slice(0, 10) === label
  )
}

function isBase100Series(
  block: Pick<Extract<ReportBlock, { kind: "series" }>, "id" | "unitCode">
) {
  return (
    block.id === "macro.commodity_normalized_performance" ||
    block.id === "crypto.normalized_performance" ||
    /base[-_ ]?100|normalized/i.test(block.unitCode ?? "")
  )
}

function ReportBlockView({
  block,
  index,
}: {
  block: ReportBlock
  index: number
}) {
  const { t } = useTranslation()
  const chartColors = useChartColors()
  const animate = useEnterAnimation()
  const blockTitle =
    block.kind === "series" && block.title
      ? valueText(block.title, t)
      : t(block.titleKey)
  return (
    <motion.section
      className={`surface-panel min-w-0 p-5 ${block.kind === "series" ? "xl:col-span-2" : ""}`}
      {...reveal(animate, index)}
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="m-0 text-base font-extrabold tracking-[-0.015em] text-sea-ink">
            {blockTitle}
          </h2>
        </div>
      </div>
      {block.status !== "ok" ? (
        <p className="m-0 border-y border-line py-5 text-sm text-sea-ink-soft">
          {t("reportBlockUnavailable")}
        </p>
      ) : block.kind === "metric" ? (
        <div className="grid min-w-0 divide-y divide-line border-y border-line sm:grid-cols-3 sm:divide-x sm:divide-y-0">
          {block.metrics.map(item => (
            <div
              className="min-w-0 px-3 py-3 first:pl-0 last:pr-0 max-sm:first:pt-0 max-sm:last:pb-0 sm:first:pl-0 sm:last:pr-0"
              key={item.labelKey}
            >
              <p className="m-0 text-xs text-sea-ink-soft">
                {t(item.labelKey)}
              </p>
              <p className="mt-2 mb-0 font-mono text-[22px] font-extrabold tracking-[-0.03em] text-sea-ink tabular-nums">
                {valueText(item.value, t)}
              </p>
              {"change" in item ? (
                <p
                  className={`mt-1 mb-0 font-mono text-xs font-bold tabular-nums ${directionClass(item.change ?? null, t)}`}
                >
                  {valueText(item.change ?? null, t)}
                </p>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {block.status === "ok" && block.kind === "table" ? (
        <div className="min-w-0 max-w-full overflow-x-auto border-y border-line">
          <table className="w-full min-w-120 text-sm">
            <thead className="bg-link-hover text-xs text-sea-ink-soft">
              <tr>
                {block.columns.map((column, index) => (
                  <th
                    className={`whitespace-nowrap px-3 py-2.5 font-bold ${index === 0 ? "text-left" : "text-right"}`}
                    key={column}
                  >
                    {t(column)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr className="border-t border-line" key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td
                      className={`whitespace-nowrap px-3 py-2.5 ${cellIndex === 0 ? "font-semibold text-sea-ink" : `text-right font-mono tabular-nums ${directionClass(cell, t)}`}`}
                      key={cellIndex}
                    >
                      {valueText(cell, t)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {block.status === "ok" && block.kind === "series" ? (
        <>
          <div className="h-84 min-w-0 w-full overflow-hidden border-y border-line py-2 sm:h-96">
            <ClientOnly
              fallback={
                <div
                  className="h-full w-full animate-pulse rounded-lg bg-link-hover"
                  role="status"
                  aria-label={t("reportChartSummary")}
                />
              }
            >
              <ReactECharts
                style={{ height: "100%", width: "100%" }}
                option={{
                  animation: false,
                  aria: {
                    enabled: true,
                    description: `${blockTitle}. ${t("reportChartSummary")}`,
                  },
                  color: chartColors.series,
                  grid: { left: 58, right: 18, top: 42, bottom: 72 },
                  legend: {
                    type: "scroll",
                    top: 6,
                    textStyle: { color: chartColors.text },
                  },
                  tooltip: {
                    trigger: "axis",
                    appendToBody: true,
                    valueFormatter: (value: number | string) =>
                      `${value}${block.unitLabel ? ` ${valueText(block.unitLabel, t)}` : ""}`,
                  },
                  xAxis: {
                    type: "category",
                    data: chartCategories(block, t),
                    boundaryGap: false,
                    axisLabel: { color: chartColors.text },
                    axisLine: { lineStyle: { color: chartColors.grid } },
                  },
                  yAxis: {
                    type: "value",
                    scale: true,
                    name: isBase100Series(block)
                      ? t("reportChartBase100")
                      : block.unitLabel
                        ? valueText(block.unitLabel, t)
                        : block.unitCode,
                    nameTextStyle: { color: chartColors.text },
                    axisLabel: { color: chartColors.text },
                    splitLine: {
                      lineStyle: { color: chartColors.grid, type: "dashed" },
                    },
                  },
                  dataZoom: [
                    { type: "inside", start: 0, end: 100 },
                    {
                      type: "slider",
                      height: 18,
                      bottom: 12,
                      borderColor: chartColors.grid,
                      textStyle: { color: chartColors.text },
                    },
                  ],
                  series: block.series.map((line, index) => ({
                    name: valueText(line.label, t),
                    type: "line",
                    data: chartDataForCategories(
                      line,
                      chartCategories(block, t),
                      t
                    ),
                    connectNulls: false,
                    symbolSize: 6,
                    smooth: 0.18,
                    lineStyle: { width: 2 },
                    areaStyle: { color: "transparent" },
                    ...(isBase100Series(block) && index === 0
                      ? {
                          markLine: {
                            symbol: "none",
                            lineStyle: {
                              color: chartColors.grid,
                              type: "dashed",
                            },
                            label: {
                              color: chartColors.text,
                              formatter: t("reportChartBase100Reference"),
                            },
                            data: [{ yAxis: 100 }],
                          },
                        }
                      : {}),
                  })),
                }}
              />
            </ClientOnly>
          </div>
          <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-sea-ink-soft">
            <div className="flex gap-1">
              <dt>{t("reportChartUnit")}</dt>
              <dd className="m-0 text-sea-ink">
                {block.unitLabel
                  ? valueText(block.unitLabel, t)
                  : (block.unitCode ?? "—")}
              </dd>
            </div>
          </dl>
          <div className="sr-only">
            <table>
              <caption>{t("reportChartSummary")}</caption>
              <tbody>
                {block.series.flatMap(line =>
                  line.points.map(point => (
                    <tr key={`${line.id}-${valueText(point.label, t)}`}>
                      <th>
                        {valueText(line.label, t)} {valueText(point.label, t)}
                      </th>
                      <td>{point.value ?? "—"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </motion.section>
  )
}

export function ReportErrorScreen({ error }: { error: Error }) {
  void error
  const { t } = useTranslation()
  const router = useRouter()
  return (
    <section
      className="surface-panel border-market-up/35 p-10 text-center"
      role="alert"
    >
      <h2 className="mt-0 text-2xl">{t("reportsErrorTitle")}</h2>
      <p className="mx-auto max-w-xl text-sea-ink-soft">
        {t("reportsErrorDescription")}
      </p>
      <button type="button" onClick={() => void router.invalidate()}>
        {t("retry")}
      </button>
    </section>
  )
}

export function ReportNotLaunchedScreen() {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel p-10 text-center"
      role="status"
      aria-live="polite"
    >
      <h2 className="mt-0 text-xl">{t("reportNotLaunchedTitle")}</h2>
      <p className="mb-0 text-sm text-sea-ink-soft">
        {t("reportNotLaunchedDescription")}
      </p>
    </section>
  )
}

export function ReportNotGeneratedScreen() {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel border-market-caution/35 p-10 text-center"
      role="status"
      aria-live="polite"
    >
      <p className="m-0 text-sm text-sea-ink-soft">
        {t("reportBlockUnavailable")}
      </p>
    </section>
  )
}
