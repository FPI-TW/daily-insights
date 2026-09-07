import type {
  AnalystViewpoint,
  LatestNews,
  Locale,
} from "@daily-insights/api-client"
import {
  Await,
  createFileRoute,
  notFound,
  redirect,
} from "@tanstack/react-router"
import { Suspense } from "react"
import {
  TaiwanIndexHistoryChart,
  TaiwanIndexHistoryLoading,
} from "#/components/TaiwanIndexHistoryChart"
import {
  MacroDashboard,
  MacroDashboardLoading,
} from "#/components/MacroDashboard"
import { getMacroDashboard } from "#/lib/macro-dashboard.functions"
import type { MacroDashboardData } from "#/lib/macro-dashboard"
import { getVisibleMarkets } from "#/lib/markets"
import { DailyNews } from "#/components/DailyNews"
import {
  IndexHistoryChart,
  IndexHistoryLoading,
} from "#/components/IndexHistoryChart"
import {
  UsIndexPerformanceTable,
  UsIndexPerformanceTableLoading,
} from "#/components/UsIndexPerformanceTable"
import {
  VixHistoryChart,
  VixHistoryLoading,
} from "#/components/VixHistoryChart"
import {
  MarketViewpoint,
  ReportDetail,
  ReportErrorScreen,
  ReportLoadingScreen,
  ReportNotGeneratedScreen,
  ReportNotLaunchedScreen,
} from "#/components/Reports"
import { getTodayAnalystViewpoints } from "#/lib/analyst-viewpoints"
import { getMarketNews } from "#/lib/news"
import {
  isNewsMarketCode,
  type NewsMarketCode,
} from "#/lib/provisional-reports"
import { getReportDetail } from "#/lib/reports"
import {
  chartMarketCodes,
  getMarketIndexHistory,
  getMarketIndexMovingAverages,
  getVixHistory,
  twoYearTaipeiRange,
  type IndexMovingAverageMap,
  type MarketIndexHistory,
  type VixHistory,
} from "#/lib/indices"
import { useChatPageContext } from "#/components/PageContextChat"
import {
  TaiwanInstitutionalFlows,
  TaiwanInstitutionalFlowsLoading,
} from "#/components/TaiwanInstitutionalFlows"
import {
  getTaiwanInstitutionalFlows,
  getTaiwanInstitutionalStocks,
  institutionalFlowRange,
  type TaiwanInstitutionalData,
} from "#/lib/institutional-flows"

type ReportResult = Awaited<ReturnType<typeof getReportDetail>>
type MarketPage = {
  report: Exclude<ReportResult, { kind: "not-found" }>
  news: { marketCode: NewsMarketCode; latest: LatestNews | null } | null
  viewpoint: AnalystViewpoint | null
  macroDashboard: Promise<MacroDashboardData | null> | null
  indexHistory: Promise<MarketIndexHistory | null> | null
  indexMovingAverages: Promise<IndexMovingAverageMap> | null
  institutionalData: Promise<TaiwanInstitutionalData> | null
  vixHistory: Promise<VixHistory | null> | null
}

export const INDEX_HISTORY_DEADLINE_MS = 10_000
export const INDEX_MOVING_AVERAGES_DEADLINE_MS = 10_000
export const INSTITUTIONAL_DATA_DEADLINE_MS = 10_000
export const VIX_HISTORY_DEADLINE_MS = 10_000

export function withIndexHistoryDeadline(
  history: Promise<MarketIndexHistory>,
  deadlineMs = INDEX_HISTORY_DEADLINE_MS
): Promise<MarketIndexHistory | null> {
  return new Promise(resolve => {
    const deadline = setTimeout(() => resolve(null), deadlineMs)
    void history.then(
      value => {
        clearTimeout(deadline)
        resolve(value)
      },
      () => {
        clearTimeout(deadline)
        resolve(null)
      }
    )
  })
}

export function withMovingAverageDeadline(
  movingAverages: Promise<IndexMovingAverageMap>,
  deadlineMs = INDEX_MOVING_AVERAGES_DEADLINE_MS
): Promise<IndexMovingAverageMap> {
  return new Promise(resolve => {
    const deadline = setTimeout(() => resolve({}), deadlineMs)
    void movingAverages.then(
      value => {
        clearTimeout(deadline)
        resolve(value)
      },
      () => {
        clearTimeout(deadline)
        resolve({})
      }
    )
  })
}

export function withInstitutionalDataDeadline(
  data: Promise<TaiwanInstitutionalData>,
  deadlineMs = INSTITUTIONAL_DATA_DEADLINE_MS
): Promise<TaiwanInstitutionalData> {
  return new Promise(resolve => {
    const deadline = setTimeout(
      () => resolve({ flows: null, stocks: null }),
      deadlineMs
    )
    void data.then(
      value => {
        clearTimeout(deadline)
        resolve(value)
      },
      () => {
        clearTimeout(deadline)
        resolve({ flows: null, stocks: null })
      }
    )
  })
}

export function withVixHistoryDeadline(
  history: Promise<VixHistory>,
  deadlineMs = VIX_HISTORY_DEADLINE_MS
): Promise<VixHistory | null> {
  return new Promise(resolve => {
    const deadline = setTimeout(() => resolve(null), deadlineMs)
    void history.then(
      value => {
        clearTimeout(deadline)
        resolve(value)
      },
      () => {
        clearTimeout(deadline)
        resolve(null)
      }
    )
  })
}

// The report is the primary content; market news is secondary and degrades to
// its unavailable state instead of failing the route.
export async function loadMarketPage({
  params,
  context,
}: {
  params: { marketCode: string }
  context: { locale: Locale }
}): Promise<MarketPage> {
  if (params.marketCode === "forex") {
    const markets = await getVisibleMarkets({ data: context.locale })
    if (markets.some(market => market.code === "global_macro_bonds")) {
      throw redirect({
        to: "/$locale/reports/$marketCode",
        params: { locale: context.locale, marketCode: "global_macro_bonds" },
        replace: true,
      })
    }
  }
  const macroDashboard =
    params.marketCode === "global_macro_bonds"
      ? getMacroDashboard().catch(() => null)
      : null
  const newsMarket = isNewsMarketCode(params.marketCode)
    ? params.marketCode
    : null
  const supportsIndexChart = (chartMarketCodes as readonly string[]).includes(
    params.marketCode
  )
  const indexRange = supportsIndexChart ? twoYearTaipeiRange() : null
  const indexHistory = supportsIndexChart
    ? withIndexHistoryDeadline(
        getMarketIndexHistory({
          data: {
            marketCode: params.marketCode as "us_equity" | "tw_equity",
            range: indexRange!,
          },
        })
      )
    : null
  const indexMovingAverages = supportsIndexChart
    ? withMovingAverageDeadline(
        getMarketIndexMovingAverages({
          data: {
            marketCode: params.marketCode as "us_equity" | "tw_equity",
            range: indexRange!,
          },
        })
      )
    : null
  const institutionalData =
    params.marketCode === "tw_equity" && indexRange
      ? withInstitutionalDataDeadline(
          Promise.allSettled([
            getTaiwanInstitutionalFlows({
              data: institutionalFlowRange(indexRange.end),
            }),
            getTaiwanInstitutionalStocks({
              data: { date: indexRange.end, locale: context.locale },
            }),
          ]).then(([flows, stocks]) => ({
            flows: flows.status === "fulfilled" ? flows.value : null,
            stocks: stocks.status === "fulfilled" ? stocks.value : null,
          }))
        )
      : null
  const vixHistory =
    params.marketCode === "us_equity" && indexRange
      ? withVixHistoryDeadline(getVixHistory({ data: { range: indexRange } }))
      : null
  const [report, news, viewpoints] = await Promise.allSettled([
    getReportDetail({
      data: { marketCode: params.marketCode, locale: context.locale },
    }),
    newsMarket
      ? getMarketNews({
          data: { locale: context.locale, marketCode: newsMarket },
        })
      : Promise.resolve(null),
    getTodayAnalystViewpoints(),
  ])
  if (report.status === "rejected") throw report.reason
  if (report.value.kind === "not-found") throw notFound()
  // The analyst's bullets are secondary like the news: absent, not fatal.
  const viewpoint =
    viewpoints.status === "fulfilled"
      ? (viewpoints.value.find(
          item => item.market_code === params.marketCode
        ) ?? null)
      : null
  return {
    report: report.value,
    news: newsMarket
      ? {
          marketCode: newsMarket,
          latest: news.status === "fulfilled" ? news.value : null,
        }
      : null,
    viewpoint,
    macroDashboard,
    indexHistory,
    indexMovingAverages,
    institutionalData,
    vixHistory,
  }
}

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/$marketCode"
)({
  loader: loadMarketPage,
  pendingComponent: MarketPageLoading,
  errorComponent: ReportErrorScreen,
  component: ReportPage,
})

function ReportPage() {
  const {
    report,
    news,
    viewpoint,
    macroDashboard,
    indexHistory,
    indexMovingAverages,
    institutionalData,
    vixHistory,
  } = Route.useLoaderData()
  const { marketCode } = Route.useParams()
  const { locale } = Route.useRouteContext()
  useChatPageContext(
    report.kind === "report" && report.report.publicationId
      ? { kind: "report_detail", publication_id: report.report.publicationId }
      : null
  )
  return (
    <>
      {(report.kind !== "report" || macroDashboard) && viewpoint ? (
        <MarketViewpoint viewpoint={viewpoint} />
      ) : null}
      {macroDashboard ? (
        <Suspense fallback={<MacroDashboardLoading />}>
          <Await promise={macroDashboard}>
            {data => <MacroDashboard data={data} locale={locale} />}
          </Await>
        </Suspense>
      ) : report.kind === "not-generated" ? (
        <ReportNotGeneratedScreen
          locale={locale}
          marketCode={report.marketCode}
        />
      ) : report.kind === "not-launched" ? (
        <ReportNotLaunchedScreen
          locale={locale}
          marketCode={report.marketCode}
        />
      ) : (
        <ReportDetail
          locale={locale}
          report={report.report}
          viewpoint={viewpoint}
          leadingBlock={
            marketCode === "us_equity" && indexHistory ? (
              <Suspense fallback={<UsIndexPerformanceTableLoading />}>
                <Await promise={indexHistory}>
                  {history => (
                    <UsIndexPerformanceTable
                      history={history}
                      locale={locale}
                    />
                  )}
                </Await>
              </Suspense>
            ) : null
          }
        />
      )}
      {(chartMarketCodes as readonly string[]).includes(marketCode) &&
      indexHistory ? (
        <Suspense
          fallback={
            marketCode === "tw_equity" ? (
              <TaiwanIndexHistoryLoading />
            ) : (
              <IndexHistoryLoading />
            )
          }
        >
          <Await promise={indexHistory}>
            {history => (
              <>
                {marketCode === "tw_equity" ? (
                  <TaiwanIndexHistoryChart
                    history={history}
                    locale={locale}
                    movingAverages={indexMovingAverages}
                  />
                ) : (
                  <IndexHistoryChart
                    history={history}
                    locale={locale}
                    movingAverages={indexMovingAverages}
                  />
                )}
                {marketCode === "tw_equity" && institutionalData ? (
                  <Suspense fallback={<TaiwanInstitutionalFlowsLoading />}>
                    <Await promise={institutionalData}>
                      {data => (
                        <TaiwanInstitutionalFlows
                          data={data}
                          history={history}
                          locale={locale}
                        />
                      )}
                    </Await>
                  </Suspense>
                ) : null}
              </>
            )}
          </Await>
        </Suspense>
      ) : null}
      {marketCode === "us_equity" && vixHistory ? (
        <Suspense fallback={<VixHistoryLoading />}>
          <Await promise={vixHistory}>
            {history => <VixHistoryChart history={history} locale={locale} />}
          </Await>
        </Suspense>
      ) : null}
      {news ? (
        <DailyNews
          news={news.latest}
          eyebrowKey="marketNewsEyebrow"
          titleKey={`marketNewsTitle_${news.marketCode}`}
          groupByMarket={false}
        />
      ) : null}
    </>
  )
}

function MarketPageLoading() {
  const { marketCode } = Route.useParams()
  return (
    <>
      <ReportLoadingScreen />
      {marketCode === "us_equity" ? (
        <div className="mt-6">
          <UsIndexPerformanceTableLoading />
        </div>
      ) : null}
      <IndexHistoryLoading />
      {marketCode === "us_equity" ? <VixHistoryLoading /> : null}
    </>
  )
}
