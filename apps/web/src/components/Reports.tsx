import { ResponsiveTable } from "./ResponsiveTable"
import { ClientOnly, Link, useRouter } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { motion } from "motion/react"
import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import type { AnalystViewpoint, Locale } from "@daily-insights/api-client"
import {
  directionClass,
  formatChange,
  formatTimestamp,
  formatNumber,
  literalDirection,
  unitLabel,
  type Direction,
} from "#/lib/format"
import { marketTabLabel, type NavMarket } from "#/lib/markets"
import { useChartColors } from "#/lib/chart"
import {
  type MarketCode,
  type ProvisionalReport,
  type ReportBlock,
  type ReportValue,
  type TableColumn,
} from "#/lib/provisional-reports"
import { fadeIn, reveal, useEnterAnimation } from "#/lib/motion"
import { ActiveIndicator } from "./ActiveIndicator"

type Translate = ReturnType<typeof useTranslation>["t"]

const HIDDEN_REPORT_NAV_MARKETS = new Set<MarketCode>([
  "crypto",
  "hk_equity",
  "cn_equity",
  "tw_index_derivatives",
])

function reportNavMarkets(markets: ReadonlyArray<NavMarket>) {
  return markets.filter(
    market =>
      !HIDDEN_REPORT_NAV_MARKETS.has(market.code) &&
      (market.code !== "forex" ||
        !markets.some(item => item.code === "global_macro_bonds"))
  )
}

/** Display text for a value that is not a number: translation keys and
 * pre-formatted literals. Numbers go through `formatNumber`/`formatChange`. */
function valueText(value: ReportValue | null, t: Translate) {
  if (value === null) return "—"
  if (value.kind === "translation") return t(value.key)
  return String(value.value)
}

function isCurrencyCode(unitCode: string | null | undefined) {
  return /^[a-z]{3}$/i.test(unitCode ?? "")
}

function formatValue(
  value: ReportValue | null,
  unitCode: string | null | undefined,
  locale: Locale,
  t: Translate
) {
  if (value !== null && value.kind === "number") {
    return formatNumber(value.value, unitCode, locale)
  }
  return valueText(value, t)
}

function changeOf(
  value: ReportValue | null | undefined,
  locale: Locale,
  t: Translate,
  percent = true
): { text: string; direction: Direction } {
  if (value === null || value === undefined) {
    return { text: "—", direction: "none" }
  }
  const flatLabel = t("reportChangeFlat")
  if (value.kind === "number") {
    return formatChange(value.value, locale, { percent, flatLabel })
  }
  const text = valueText(value, t)
  return { text, direction: literalDirection(text) }
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
  markets,
  activeMarket,
}: {
  locale: Locale
  markets: ReadonlyArray<NavMarket>
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
        to="/{-$locale}/reports"
        params={{ locale }}
        activeOptions={{ exact: true }}
        className={linkClass(activeMarket === undefined)}
      >
        {t("reportAllMarkets")}
      </Link>
      {reportNavMarkets(markets).map(market => (
        <Link
          key={market.code}
          to="/{-$locale}/reports/$marketCode"
          params={{ locale, marketCode: market.code }}
          className={linkClass(activeMarket === market.code)}
        >
          {marketTabLabel(t, market)}
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

/** Page frame shared by the report list and every market page: one heading
 * and one market navigation, driven by the organization's visible markets. */
export function ReportShell({
  locale,
  markets = [],
  activeMarket,
  children,
}: {
  locale: Locale
  markets?: ReadonlyArray<NavMarket>
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
      <ReportMarketNav
        locale={locale}
        markets={markets}
        activeMarket={activeMarket}
      />
      {children}
    </main>
  )
}

export function ReportList({
  locale: _locale,
  viewpoints = [],
  markets,
}: {
  locale?: Locale
  viewpoints?: ReadonlyArray<AnalystViewpoint>
  markets?: ReadonlyArray<NavMarket>
}) {
  return <AnalystViewpoints viewpoints={viewpoints} markets={markets} />
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
        <div className="h-20 rounded bg-line" />
        <div className="h-20 rounded bg-line" />
        <div className="h-20 rounded bg-line" />
      </div>
    </section>
  )
}

function AnalystViewpoints({
  viewpoints,
  markets,
}: {
  viewpoints: ReadonlyArray<AnalystViewpoint>
  markets?: ReadonlyArray<NavMarket> | undefined
}) {
  const { t } = useTranslation()
  // Viewpoints follow navigation order and only cover navigable markets when
  // the market list is known; otherwise they are shown as delivered.
  const visibleViewpoints = markets
    ? reportNavMarkets(markets).flatMap(market => {
        const viewpoint = viewpoints.find(v => v.market_code === market.code)
        return viewpoint ? [viewpoint] : []
      })
    : viewpoints
  const latest = visibleViewpoints[0]
  if (!latest) return null
  return (
    <section className="mb-6" aria-labelledby="analyst-viewpoints-title">
      <div className="mb-3">
        <h2
          id="analyst-viewpoints-title"
          className="m-0 text-lg font-extrabold tracking-[-0.02em] text-sea-ink"
        >
          {t("analystViewpointsTitle")}
        </h2>
        <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
          {t("analystViewpointsUpdated", {
            timestamp: formatTimestamp(latest.fetched_at),
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

/** Notices above the blocks: the partial-status badge and the report-level
 * caveat when the pipeline attached one. Source freshness (the API's stale
 * flag and its reason code) is an operator signal and is never shown here. */
function ReportNotices({ report }: { report: ProvisionalReport }) {
  const { t } = useTranslation()
  if (!report.caveat && report.status !== "partial") return null
  return (
    <div className="mb-4 text-xs text-sea-ink-soft">
      {report.status === "partial" ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-line px-2 py-0.5 font-bold">
            {t("reportStatusPartial")}
          </span>
        </div>
      ) : null}
      {report.caveat ? <p className="mt-1 mb-0">{report.caveat}</p> : null}
    </div>
  )
}

export function ReportDetail({
  locale = "zh-hant",
  report,
  viewpoint = null,
  afterViewpoint = null,
  leadingBlock = null,
}: {
  locale?: Locale
  report: ProvisionalReport
  viewpoint?: AnalystViewpoint | null
  // Content that reads before the numbers, directly under the analyst's
  // bullets: the US page places its market news here.
  afterViewpoint?: ReactNode
  leadingBlock?: ReactNode
}) {
  // A lone metric or table block spans the full width; half-width panels
  // only make sense when there is a second one to sit beside. The leading
  // block (the US five-index table) counts as that neighbour, so it and the
  // mega-caps block share one row.
  const narrowBlocks = report.blocks.filter(block => block.kind !== "series")
  const loneNarrowBlock = narrowBlocks.length === 1 && !leadingBlock
  return (
    <>
      <ReportNotices report={report} />
      {viewpoint ? <MarketViewpoint viewpoint={viewpoint} /> : null}
      {afterViewpoint ? <div className="mb-6">{afterViewpoint}</div> : null}
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {leadingBlock ? <div className="min-w-0">{leadingBlock}</div> : null}
        {report.blocks.map((block, index) => (
          <ReportBlockView
            block={block}
            index={index}
            locale={locale}
            fullWidth={block.kind !== "series" && loneNarrowBlock}
            key={`${block.titleKey}-${index}`}
          />
        ))}
      </div>
    </>
  )
}

/** The upstream analyst's bullets for one market, shown above the numbers so
 * every market page opens with what happened rather than only how much. */
export function MarketViewpoint({
  viewpoint,
}: {
  viewpoint: AnalystViewpoint
}) {
  const { t } = useTranslation()
  return (
    <section
      className="surface-panel mb-4 border-t-[3px] border-t-lagoon p-5"
      aria-labelledby={`viewpoint-${viewpoint.market_code}`}
    >
      <div className="mb-2">
        <h2
          id={`viewpoint-${viewpoint.market_code}`}
          className="m-0 text-base font-extrabold tracking-[-0.015em] text-sea-ink"
        >
          {t("marketViewpointTitle")}
        </h2>
        <p className="mt-1 mb-0 text-xs text-sea-ink-soft">
          {t("analystViewpointsUpdated", {
            timestamp: formatTimestamp(viewpoint.fetched_at),
          })}
        </p>
      </div>
      <ul className="m-0 grid list-disc gap-2 pl-5 text-sm leading-6 text-sea-ink">
        {viewpoint.points.map(point => (
          <li key={point}>{point}</li>
        ))}
      </ul>
    </section>
  )
}

function chartCategories(
  block: Extract<ReportBlock, { kind: "series" }>,
  t: Translate
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
  t: Translate
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

function isCommodityRatioSeries(
  block: Pick<Extract<ReportBlock, { kind: "series" }>, "id">
) {
  return block.id === "macro.commodity_ratios"
}

function columnHeading(column: TableColumn, t: Translate) {
  const label = t(column.labelKey)
  const unit = unitLabel(column.unitCode, t)
  return unit ? `${label} (${unit})` : label
}

function BlockNote({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <p className="mt-3 mb-0 text-xs leading-5 text-sea-ink-soft">
      <span className="font-bold">{t("reportCaveatLabel")}</span> {children}
    </p>
  )
}

const metricCellClass =
  "min-w-0 border-t border-line py-3 first:border-t-0 sm:border-l sm:px-3 sm:nth-[-n+3]:border-t-0 sm:nth-[3n+1]:border-l-0 sm:nth-[3n+1]:pl-0 sm:nth-[3n]:pr-0"

function ReportBlockView({
  block,
  index,
  locale,
  fullWidth = false,
}: {
  block: ReportBlock
  index: number
  locale: Locale
  fullWidth?: boolean
}) {
  const { t } = useTranslation()
  const chartColors = useChartColors()
  const animate = useEnterAnimation()
  const blockTitle =
    block.kind === "series" && block.title
      ? valueText(block.title, t)
      : t(block.titleKey)
  const caveat = block.caveat ? valueText(block.caveat, t) : null
  return (
    <motion.section
      className={`surface-panel min-w-0 p-5 ${block.kind === "series" || fullWidth ? "xl:col-span-2" : ""}`}
      {...reveal(animate, index)}
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="m-0 text-base font-extrabold tracking-[-0.015em] text-sea-ink">
            {blockTitle}
          </h2>
        </div>
      </div>
      {block.status === "missing" ? (
        // An honest "nothing today" instead of zeros or an empty panel.
        <p className="m-0 border-y border-line py-5 text-sm text-sea-ink-soft">
          {t("reportBlockMissing")}
        </p>
      ) : block.status === "error" ? (
        <p
          className="m-0 border-y border-market-caution/40 py-5 text-sm text-sea-ink-soft"
          role="status"
        >
          {t("reportBlockError")}
        </p>
      ) : block.kind === "metric" ? (
        // Cells own their borders instead of using divide-*: with more metrics
        // than columns, divide-x draws a stray left edge on the row-leading
        // cell and leaves no rule between the rows. Empty filler cells complete
        // the last row so its rules run the full width of the block.
        <div className="grid min-w-0 border-y border-line sm:grid-cols-3">
          {block.metrics.map(item => {
            const unit = unitLabel(item.unitCode, t)
            const change = changeOf(item.change, locale, t)
            return (
              <div className={metricCellClass} key={item.labelKey}>
                <p className="m-0 text-xs text-sea-ink-soft">
                  {t(item.labelKey)}
                </p>
                <p className="mt-2 mb-0 font-mono text-[22px] font-extrabold tracking-[-0.03em] text-sea-ink tabular-nums">
                  {formatValue(item.value, item.unitCode, locale, t)}
                  {/* Each metric carries its own currency: a block can mix
                      USD and EUR (gold in dollars, copper in euros). */}
                  {unit && isCurrencyCode(item.unitCode) ? (
                    <span className="ml-1.5 align-middle text-xs font-bold text-sea-ink-soft">
                      {unit}
                    </span>
                  ) : null}
                </p>
                {"change" in item ? (
                  <p
                    className={`mt-1 mb-0 font-mono text-xs font-bold tabular-nums ${directionClass(change.direction)}`}
                  >
                    {change.text}
                  </p>
                ) : null}
              </div>
            )
          })}
          {Array.from(
            { length: (3 - (block.metrics.length % 3)) % 3 },
            (_, filler) => (
              <div
                aria-hidden="true"
                className={`${metricCellClass} max-sm:hidden`}
                key={`filler-${filler}`}
              />
            )
          )}
        </div>
      ) : null}
      {block.status === "ok" && block.kind === "table" ? (
        <div className="min-w-0 max-w-full border-y border-line">
          <ResponsiveTable>
            <thead className="bg-link-hover text-xs text-sea-ink-soft">
              <tr>
                {block.columns.map((column, index) => (
                  <th
                    className={`whitespace-nowrap px-4 py-1.5 font-bold ${index === 0 ? "text-left" : "text-right"}`}
                    key={column.labelKey}
                  >
                    {columnHeading(column, t)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr className="border-t border-line" key={rowIndex}>
                  {row.map((cell, cellIndex) => {
                    if (cellIndex === 0) {
                      return (
                        <th
                          scope="row"
                          className="px-4 py-1 text-left font-semibold text-sea-ink"
                          key={cellIndex}
                        >
                          {valueText(cell, t)}
                        </th>
                      )
                    }
                    const column = block.columns[cellIndex]
                    const isChange = column?.unitCode === "percent"
                    const shown = isChange
                      ? changeOf(cell, locale, t)
                      : {
                          text: formatValue(cell, column?.unitCode, locale, t),
                          direction:
                            cell?.kind === "number"
                              ? ("none" as const)
                              : literalDirection(valueText(cell, t)),
                        }
                    return (
                      <td
                        className={`whitespace-nowrap px-4 py-1 text-right font-mono tabular-nums ${shown.direction === "none" ? "text-sea-ink" : directionClass(shown.direction)}`}
                        data-label={column ? columnHeading(column, t) : ""}
                        key={cellIndex}
                      >
                        {shown.text}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </ResponsiveTable>
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
                  grid: {
                    left: 58,
                    right: isCommodityRatioSeries(block) ? 76 : 18,
                    top: 42,
                    bottom: 72,
                  },
                  legend: {
                    type: "scroll",
                    top: 6,
                    textStyle: { color: chartColors.text },
                  },
                  tooltip: {
                    trigger: "axis",
                    appendToBody: true,
                    valueFormatter: (value: number | string) =>
                      formatNumber(value, block.unitCode, locale),
                  },
                  xAxis: {
                    type: "category",
                    data: chartCategories(block, t),
                    boundaryGap: false,
                    axisLabel: { color: chartColors.text },
                    axisLine: { lineStyle: { color: chartColors.grid } },
                  },
                  yAxis: isCommodityRatioSeries(block)
                    ? [
                        {
                          type: "value",
                          scale: true,
                          name: valueText(block.series[0]?.label ?? null, t),
                          nameTextStyle: { color: chartColors.text },
                          axisLabel: {
                            color: chartColors.text,
                            formatter: (value: number) =>
                              formatNumber(value, "ratio", locale),
                          },
                          splitLine: {
                            lineStyle: {
                              color: chartColors.grid,
                              type: "dashed",
                            },
                          },
                        },
                        {
                          type: "value",
                          scale: true,
                          position: "right",
                          name: valueText(block.series[1]?.label ?? null, t),
                          nameTextStyle: { color: chartColors.text },
                          axisLabel: {
                            color: chartColors.text,
                            formatter: (value: number) =>
                              formatNumber(value, "ratio", locale),
                          },
                          splitLine: { show: false },
                        },
                      ]
                    : {
                        type: "value",
                        scale: true,
                        name: isBase100Series(block)
                          ? t("reportChartBase100")
                          : block.unitLabel
                            ? valueText(block.unitLabel, t)
                            : (unitLabel(block.unitCode, t) ?? ""),
                        nameTextStyle: { color: chartColors.text },
                        axisLabel: { color: chartColors.text },
                        splitLine: {
                          lineStyle: {
                            color: chartColors.grid,
                            type: "dashed",
                          },
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
                    ...(isCommodityRatioSeries(block)
                      ? { yAxisIndex: index }
                      : {}),
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
          {!isCommodityRatioSeries(block) ? (
            <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-sea-ink-soft">
              <div className="flex gap-1">
                <dt>{t("reportChartUnit")}</dt>
                <dd className="m-0 text-sea-ink">
                  {block.unitLabel
                    ? valueText(block.unitLabel, t)
                    : (unitLabel(block.unitCode, t) ?? "—")}
                </dd>
              </div>
            </dl>
          ) : null}
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
                      <td>
                        {point.value === null
                          ? "—"
                          : formatNumber(point.value, block.unitCode, locale)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
      {caveat ? <BlockNote>{caveat}</BlockNote> : null}
    </motion.section>
  )
}

export function ReportErrorScreen({ error }: { error: Error }) {
  void error
  const { t } = useTranslation()
  const router = useRouter()
  return (
    <main className="page-shell">
      <section
        className="surface-panel border-market-up/35 p-10 text-center"
        role="alert"
      >
        <h1 className="mt-0 text-2xl">{t("reportsErrorTitle")}</h1>
        <p className="mx-auto max-w-xl text-sea-ink-soft">
          {t("reportsErrorDescription")}
        </p>
        <button type="button" onClick={() => void router.invalidate()}>
          {t("retry")}
        </button>
      </section>
    </main>
  )
}

// The heading and market navigation come from the reports layout, so these
// states render only their panel; rendering a second nav here is what
// produced the duplicated tab bar on the Taiwan page.
export function ReportNotLaunchedScreen({
  marketCode,
}: {
  locale?: Locale
  marketCode: MarketCode
}) {
  const { t } = useTranslation()
  if (marketCode === "tw_equity") return null
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

export function ReportNotGeneratedScreen({
  marketCode,
}: {
  locale?: Locale
  marketCode: MarketCode
}) {
  void marketCode
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
