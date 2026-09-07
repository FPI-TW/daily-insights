import { ClientOnly, Link, useRouter } from "@tanstack/react-router"
import ReactECharts from "echarts-for-react"
import { motion } from "motion/react"
import type { ReactNode } from "react"
import { useTranslation } from "react-i18next"
import type { AnalystViewpoint, Locale } from "@daily-insights/api-client"
import {
  directionClass,
  formatChange,
  formatIsoDate,
  formatNumber,
  literalDirection,
  numberLocales,
  unitLabel,
  type Direction,
} from "#/lib/format"
import type { NavMarket } from "#/lib/markets"
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
  const { t, i18n } = useTranslation()
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
      {markets
        .filter(
          market =>
            market.code !== "forex" ||
            !markets.some(item => item.code === "global_macro_bonds")
        )
        .map(market => (
          <Link
            key={market.code}
            to="/$locale/reports/$marketCode"
            params={{ locale, marketCode: market.code }}
            className={linkClass(activeMarket === market.code)}
          >
            {i18n.exists(`reportMarketShort_${market.code}`)
              ? t(`reportMarketShort_${market.code}`)
              : market.name}
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

// Rendered on the server and in the browser: the formatter is pinned to the
// route locale and the Taipei zone so both produce the same text (a locale or
// zone taken from the environment differs between them and breaks hydration).
function viewpointTimestamp(language: string) {
  const locale = (
    language in numberLocales ? language : "zh-hant"
  ) as keyof typeof numberLocales
  return new Intl.DateTimeFormat(numberLocales[locale], {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Taipei",
  })
}

function AnalystViewpoints({
  viewpoints,
  markets,
}: {
  viewpoints: ReadonlyArray<AnalystViewpoint>
  markets?: ReadonlyArray<NavMarket> | undefined
}) {
  const { t, i18n } = useTranslation()
  // Viewpoints follow navigation order and only cover navigable markets when
  // the market list is known; otherwise they are shown as delivered.
  const visibleViewpoints = markets
    ? markets.flatMap(market => {
        const viewpoint = viewpoints.find(v => v.market_code === market.code)
        return viewpoint ? [viewpoint] : []
      })
    : viewpoints
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
            timestamp: viewpointTimestamp(i18n.language).format(
              new Date(latest.fetched_at)
            ),
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

/** Freshness line above the blocks: the data cut-off, a stale marker with
 * its reason, and the report-level caveat when the pipeline attached one. */
function ReportFreshness({
  report,
  locale,
}: {
  report: ProvisionalReport
  locale: Locale
}) {
  const { t } = useTranslation()
  if (!report.sourceDate && !report.stale && !report.caveat) return null
  return (
    <div className="mb-4 text-xs text-sea-ink-soft">
      <div className="flex flex-wrap items-center gap-2">
        {report.sourceDate ? (
          <span>
            {t("reportSourceAsOf", {
              date: formatIsoDate(report.sourceDate, locale),
            })}
          </span>
        ) : null}
        {report.stale ? (
          <span className="rounded-full border border-market-caution/50 bg-market-caution/10 px-2 py-0.5 font-bold text-market-caution">
            {t("reportStale")}
          </span>
        ) : null}
        {report.status === "partial" ? (
          <span className="rounded-full border border-line px-2 py-0.5 font-bold">
            {t("reportStatusPartial")}
          </span>
        ) : null}
      </div>
      {report.stale && report.staleReason ? (
        <p className="mt-1 mb-0">{report.staleReason}</p>
      ) : null}
      {report.caveat ? <p className="mt-1 mb-0">{report.caveat}</p> : null}
    </div>
  )
}

export function ReportDetail({
  locale = "zh-hant",
  report,
  viewpoint = null,
}: {
  locale?: Locale
  report: ProvisionalReport
  viewpoint?: AnalystViewpoint | null
}) {
  // A lone metric or table block spans the full width; half-width panels
  // only make sense when there is a second one to sit beside.
  const narrowBlocks = report.blocks.filter(block => block.kind !== "series")
  return (
    <>
      <ReportFreshness report={report} locale={locale} />
      {viewpoint ? <MarketViewpoint viewpoint={viewpoint} /> : null}
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {report.blocks.map((block, index) => (
          <ReportBlockView
            block={block}
            index={index}
            locale={locale}
            fullWidth={block.kind !== "series" && narrowBlocks.length === 1}
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
  const { t, i18n } = useTranslation()
  return (
    <section
      className="surface-panel mb-4 border-t-[3px] border-t-lagoon p-5"
      aria-labelledby={`viewpoint-${viewpoint.market_code}`}
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-3">
        <h2
          id={`viewpoint-${viewpoint.market_code}`}
          className="m-0 text-base font-extrabold tracking-[-0.015em] text-sea-ink"
        >
          {t("marketViewpointTitle")}
        </h2>
        <p className="m-0 text-xs text-sea-ink-soft">
          {t("analystViewpointsUpdated", {
            timestamp: viewpointTimestamp(i18n.language).format(
              new Date(viewpoint.fetched_at)
            ),
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

export type OverviewEntry = {
  summary: ProvisionalReport
  detail: ProvisionalReport | null
}

type OverviewFigure = {
  label: string
  value: string
  change: { text: string; direction: Direction } | null
}

/** Up to three headline figures from a report: its first metric block, or
 * the first rows of its first table. */
function overviewFigures(
  report: ProvisionalReport,
  locale: Locale,
  t: Translate
): OverviewFigure[] {
  for (const block of report.blocks) {
    if (block.status !== "ok") continue
    if (block.kind === "metric") {
      return block.metrics.slice(0, 3).map(item => ({
        label: t(item.labelKey),
        value: formatValue(item.value, item.unitCode, locale, t),
        change: "change" in item ? changeOf(item.change, locale, t) : null,
      }))
    }
    if (block.kind === "table") {
      // Column 0 is the row label; the price is the first later column that
      // is not a percentage.
      const price = block.columns.findIndex(
        (column, index) => index > 0 && column.unitCode !== "percent"
      )
      const change = block.columns.findIndex(
        column => column.unitCode === "percent"
      )
      return block.rows.slice(0, 3).map(row => {
        const priceCell = price > 0 ? (row[price] ?? null) : null
        const changeCell = change > 0 ? (row[change] ?? null) : null
        return {
          label: valueText(row[0] ?? null, t),
          value: formatValue(
            priceCell,
            block.columns[price]?.unitCode,
            locale,
            t
          ),
          change: change > 0 ? changeOf(changeCell, locale, t) : null,
        }
      })
    }
  }
  return []
}

/** Entry cards for every launched report so the index page carries the
 * numbers, not only links to them. */
export function ReportOverview({
  locale,
  entries,
}: {
  locale: Locale
  entries: ReadonlyArray<OverviewEntry>
}) {
  const { t } = useTranslation()
  if (entries.length === 0) return null
  return (
    <section className="mb-6" aria-labelledby="report-overview-title">
      <h2
        id="report-overview-title"
        className="mt-0 mb-3 text-lg font-extrabold tracking-[-0.02em] text-sea-ink"
      >
        {t("reportOverviewTitle")}
      </h2>
      <div className="grid gap-3 md:grid-cols-3">
        {entries.map(({ summary, detail }) => {
          const figures = detail ? overviewFigures(detail, locale, t) : []
          return (
            <article
              key={summary.marketCode}
              className="surface-panel flex flex-col gap-3 p-4"
              aria-label={t(`reportMarket_${summary.marketCode}`)}
            >
              <div className="flex items-baseline justify-between gap-2">
                <h3 className="m-0 text-sm font-extrabold text-sea-ink">
                  {t(`reportMarket_${summary.marketCode}`)}
                </h3>
                <span className="text-xs text-sea-ink-soft">
                  {summary.sourceDate
                    ? formatIsoDate(summary.sourceDate, locale)
                    : ""}
                </span>
              </div>
              {figures.length > 0 ? (
                <dl className="m-0 grid gap-2">
                  {figures.map(figure => (
                    <div
                      key={figure.label}
                      className="flex items-baseline justify-between gap-3 text-sm"
                    >
                      <dt className="min-w-0 truncate text-sea-ink-soft">
                        {figure.label}
                      </dt>
                      <dd className="m-0 flex shrink-0 items-baseline gap-2 font-mono tabular-nums">
                        <span className="font-bold text-sea-ink">
                          {figure.value}
                        </span>
                        {figure.change ? (
                          <span
                            className={`text-xs font-bold ${directionClass(figure.change.direction)}`}
                          >
                            {figure.change.text}
                          </span>
                        ) : null}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p className="m-0 text-sm text-sea-ink-soft">
                  {t("reportOverviewUnavailable")}
                </p>
              )}
              <Link
                to="/$locale/reports/$marketCode"
                params={{ locale, marketCode: summary.marketCode }}
                className="mt-auto text-sm font-bold text-lagoon no-underline"
              >
                {t("reportOverviewOpen")}
              </Link>
            </article>
          )
        })}
      </div>
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
        <div className="min-w-0 max-w-full overflow-x-auto border-y border-line">
          <table className="w-full min-w-120 text-sm">
            <thead className="bg-link-hover text-xs text-sea-ink-soft">
              <tr>
                {block.columns.map((column, index) => (
                  <th
                    className={`whitespace-nowrap px-3 py-2.5 font-bold ${index === 0 ? "text-left" : "text-right"}`}
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
                        <td
                          className="whitespace-nowrap px-3 py-2.5 font-semibold text-sea-ink"
                          key={cellIndex}
                        >
                          {valueText(cell, t)}
                        </td>
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
                        className={`whitespace-nowrap px-3 py-2.5 text-right font-mono tabular-nums ${shown.direction === "none" ? "text-sea-ink" : directionClass(shown.direction)}`}
                        key={cellIndex}
                      >
                        {shown.text}
                      </td>
                    )
                  })}
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
  void marketCode
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
