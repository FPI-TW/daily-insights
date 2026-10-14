import { createFileRoute, notFound, useNavigate } from "@tanstack/react-router"
import {
  marketCodeSchema,
  launchMarketCodeSchema,
  type Locale,
  type MarketCode,
} from "@daily-insights/api-client"
import { useEffect, useRef, useState } from "react"
import { DailyNews, DailyNewsLoading } from "#/components/DailyNews"
import {
  AnalystViewpointsLoading,
  MarketViewpoint,
  ReportErrorScreen,
  ReportLoadingScreen,
} from "#/components/Reports"
import { ReportInformation } from "#/components/MarketInformation"
import { MarketQuerySection } from "#/components/MarketQuerySection"
import { marketQueries, useMarketQuery } from "#/lib/market-queries"
import { isNewsMarketCode } from "#/lib/provisional-reports"
import {
  indexChartSymbolsForMarket,
  twoYearTaipeiRange,
  type VolatilitySymbol,
} from "#/lib/indices"
import { institutionalFlowRange } from "#/lib/institutional-flows"
import { useChatPageContext } from "#/components/PageContextChat"
import {
  IndexHistoryChart,
  IndexHistoryLoading,
} from "#/components/IndexHistoryChart"
import {
  TaiwanIndexHistoryChart,
  TaiwanIndexHistoryLoading,
} from "#/components/TaiwanIndexHistoryChart"
import {
  TaiwanInstitutionalFlowsLoading,
  FlowPanel,
  StocksPanel,
} from "#/components/TaiwanInstitutionalFlows"
import { DashboardSection } from "#/components/DashboardPrimitives"
import { HkIndexCards, HkIndexCardsLoading } from "#/components/HkIndexCards"
import {
  HkSectorHeatmap,
  HkSectorHeatmapLoading,
} from "#/components/HkSectorHeatmap"
import {
  IndexPerformanceTable,
  IndexPerformanceTableLoading,
} from "#/components/IndexPerformanceTable"
import {
  VixHistoryChart,
  VixHistoryLoading,
} from "#/components/VixHistoryChart"
import {
  MacroDashboard,
  MacroDashboardLoading,
} from "#/components/MacroDashboard"

// What each market page renders besides its morning report. Keyed on every
// market code, so adding one is a type error until this says what it shows,
// and a market with any block of its own hides the "not launched" notice.
type PageBlocks = {
  macro?: true
  chart?: "index" | "taiwan"
  bias?: true
  performance?: readonly string[]
  volatility?: VolatilitySymbol
  hkEquity?: true
  institutional?: true
}
const PAGE_BLOCKS = {
  global_macro_bonds: { macro: true },
  forex: {},
  crypto: {},
  us_equity: {
    chart: "index",
    performance: ["^GSPC", "^NDX", "^DJI", "^SOX", "^RUT"],
    volatility: "^VIX",
  },
  hk_equity: {
    hkEquity: true,
    chart: "index",
    performance: indexChartSymbolsForMarket("hk_equity"),
    bias: true,
    volatility: "^HSIL",
  },
  cn_equity: {},
  tw_equity: { chart: "taiwan", institutional: true },
  tw_index_derivatives: {},
} satisfies Record<MarketCode, PageBlocks>

export const Route = createFileRoute(
  "/{-$locale}/_customer/reports/$marketCode"
)({
  beforeLoad: ({ params }) => {
    if (!marketCodeSchema.safeParse(params.marketCode).success) throw notFound()
  },
  errorComponent: ReportErrorScreen,
  component: ReportPage,
})
function ReportPage() {
  const { marketCode: rawCode } = Route.useParams()
  const marketCode = marketCodeSchema.parse(rawCode)
  const { locale, user } = Route.useRouteContext()
  const navigate = useNavigate()
  const queries = marketQueries(user, locale)
  const markets = useMarketQuery(queries.markets(), locale)
  const mergedForex =
    marketCode === "forex" &&
    markets.data?.some(m => m.code === "global_macro_bonds")
  const ready =
    marketCode !== "forex" || (markets.data !== undefined && !mergedForex)
  const reportReady =
    ready &&
    (launchMarketCodeSchema.safeParse(marketCode).success ||
      markets.data !== undefined)
  const redirected = useRef(false)
  useEffect(() => {
    if (mergedForex && !redirected.current) {
      redirected.current = true
      void navigate({
        to: "/{-$locale}/reports/$marketCode",
        params: { locale, marketCode: "global_macro_bonds" },
        replace: true,
      })
    }
    if (marketCode !== "forex") redirected.current = false
  }, [locale, marketCode, mergedForex, navigate])
  const [range] = useState(() => twoYearTaipeiRange())
  const blocks: PageBlocks = PAGE_BLOCKS[marketCode]
  const isChart = blocks.chart !== undefined
  const performanceSymbols = blocks.performance
  const hasOwnContent = Object.keys(blocks).length > 0
  const report = useMarketQuery(queries.report(marketCode), locale, reportReady)
  const news = useMarketQuery(
    queries.news(marketCode),
    locale,
    ready && isNewsMarketCode(marketCode)
  )
  const viewpoints = useMarketQuery(queries.viewpoints(), locale, ready)
  const macro = useMarketQuery(
    queries.macro(),
    locale,
    ready && blocks.macro === true
  )
  const history = useMarketQuery(
    queries.history(marketCode, range),
    locale,
    ready && isChart
  )
  const averages = useMarketQuery(
    queries.averages(marketCode, range),
    locale,
    ready && isChart
  )
  const flows = useMarketQuery(
    queries.flows(institutionalFlowRange(range.end)),
    locale,
    ready && blocks.institutional === true
  )
  const stocks = useMarketQuery(
    queries.stocks(range.end),
    locale,
    ready && blocks.institutional === true
  )
  const hkIndices = useMarketQuery(
    queries.latestIndices(),
    locale,
    ready && blocks.hkEquity === true
  )
  const hkHeatmap = useMarketQuery(
    queries.hkSectorHeatmap(),
    locale,
    ready && blocks.hkEquity === true
  )
  useChatPageContext(
    report.data?.kind === "report" && report.data.report.publicationId
      ? {
          kind: "report_detail",
          publication_id: report.data.report.publicationId,
        }
      : null
  )
  if (
    report.data?.kind === "not-found" ||
    (!launchMarketCodeSchema.safeParse(marketCode).success &&
      markets.data &&
      !markets.data.some(m => m.code === marketCode))
  )
    throw notFound()
  const viewpoint = viewpoints.data?.find(
    item => item.market_code === marketCode
  )
  const performanceTable = performanceSymbols ? (
    <MarketQuerySection
      query={history}
      loading={<IndexPerformanceTableLoading marketCode={marketCode} />}
    >
      {data => (
        <IndexPerformanceTable
          history={data ?? null}
          locale={locale}
          marketCode={marketCode}
          symbols={performanceSymbols}
        />
      )}
    </MarketQuerySection>
  ) : null
  // The US table takes its own column beside the morning report; the Hong
  // Kong table stacks under the index cards instead.
  const sideBySide = performanceTable !== null && !blocks.hkEquity
  return (
    <>
      {isNewsMarketCode(marketCode) ? (
        <MarketQuerySection query={news} loading={<DailyNewsLoading />}>
          {data => (
            <DailyNews
              news={data ?? null}
              eyebrowKey="marketNewsEyebrow"
              titleKey={`marketNewsTitle_${marketCode}`}
              groupByMarket={false}
            />
          )}
        </MarketQuerySection>
      ) : null}
      <MarketQuerySection
        query={viewpoints}
        loading={<AnalystViewpointsLoading />}
      >
        {() => (viewpoint ? <MarketViewpoint viewpoint={viewpoint} /> : null)}
      </MarketQuerySection>
      {blocks.macro ? (
        <MarketQuerySection query={macro} loading={<MacroDashboardLoading />}>
          {data => <MacroDashboard data={data ?? null} locale={locale} />}
        </MarketQuerySection>
      ) : (
        <div
          className={
            sideBySide ? "grid min-w-0 gap-4 xl:grid-cols-2" : "min-w-0"
          }
        >
          {sideBySide ? (
            <div className="flex min-w-0 flex-col">{performanceTable}</div>
          ) : null}
          <div className="min-w-0">
            {blocks.hkEquity ? (
              <MarketQuerySection
                query={hkIndices}
                loading={<HkIndexCardsLoading />}
              >
                {data => <HkIndexCards bars={data ?? null} locale={locale} />}
              </MarketQuerySection>
            ) : null}
            {blocks.hkEquity ? (
              <div className="mt-4 min-w-0">{performanceTable}</div>
            ) : null}
            <MarketQuerySection
              query={report}
              loading={<ReportLoadingScreen />}
            >
              {data =>
                data && data.kind !== "not-found" ? (
                  <ReportInformation
                    locale={locale}
                    report={data}
                    hideNotLaunched={hasOwnContent}
                  />
                ) : null
              }
            </MarketQuerySection>
          </div>
        </div>
      )}
      {isChart ? (
        <>
          <MarketQuerySection
            query={history}
            loading={
              blocks.chart === "taiwan" ? (
                <TaiwanIndexHistoryLoading />
              ) : (
                <IndexHistoryLoading />
              )
            }
          >
            {data =>
              blocks.chart === "taiwan" ? (
                <TaiwanIndexHistoryChart
                  history={data ?? null}
                  locale={locale}
                  movingAverages={averages.data ?? null}
                  movingAveragesPending={averages.isPending}
                />
              ) : (
                <IndexHistoryChart
                  history={data ?? null}
                  locale={locale}
                  movingAverages={averages.data ?? null}
                  movingAveragesPending={averages.isPending}
                  bias={blocks.bias === true}
                />
              )
            }
          </MarketQuerySection>
          <MarketQuerySection query={averages} loading={null}>
            {() => null}
          </MarketQuerySection>
        </>
      ) : null}
      {blocks.volatility ? (
        <VolatilityBlock
          symbol={blocks.volatility}
          range={range}
          queries={queries}
          locale={locale}
          ready={ready}
        />
      ) : null}
      {blocks.institutional ? (
        <div className="mt-6 min-w-0">
          <DashboardSection>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,560px),1fr))] items-start gap-6">
              <div className="min-w-0">
                <MarketQuerySection
                  query={flows}
                  loading={<TaiwanInstitutionalFlowsLoading />}
                >
                  {data => (
                    <FlowPanel
                      flows={data ?? null}
                      history={history.data ?? null}
                      locale={locale}
                    />
                  )}
                </MarketQuerySection>
              </div>
              <div className="min-w-0">
                <MarketQuerySection
                  query={stocks}
                  loading={<TaiwanInstitutionalFlowsLoading />}
                >
                  {data => (
                    <StocksPanel stocks={data ?? null} locale={locale} />
                  )}
                </MarketQuerySection>
              </div>
            </div>
          </DashboardSection>
        </div>
      ) : null}
      {blocks.hkEquity ? (
        <MarketQuerySection
          query={hkHeatmap}
          loading={<HkSectorHeatmapLoading />}
        >
          {data => <HkSectorHeatmap heatmap={data ?? null} locale={locale} />}
        </MarketQuerySection>
      ) : null}
    </>
  )
}

// Mounted only on pages that declare a volatility index, so other markets
// never register its queries.
function VolatilityBlock({
  symbol,
  range,
  queries,
  locale,
  ready,
}: {
  symbol: VolatilitySymbol
  range: { start: string; end: string }
  queries: ReturnType<typeof marketQueries>
  locale: Locale
  ready: boolean
}) {
  const vix = useMarketQuery(queries.vix(symbol, range), locale, ready)
  const vixAverages = useMarketQuery(
    queries.vixAverages(symbol, range),
    locale,
    ready
  )
  return (
    <>
      <MarketQuerySection
        query={vix}
        loading={<VixHistoryLoading symbol={symbol} />}
      >
        {data => (
          <VixHistoryChart
            symbol={symbol}
            history={
              data ? { ...data, indicators: vixAverages.data ?? null } : null
            }
            locale={locale}
          />
        )}
      </MarketQuerySection>
      <MarketQuerySection query={vixAverages} loading={null}>
        {() => null}
      </MarketQuerySection>
    </>
  )
}
