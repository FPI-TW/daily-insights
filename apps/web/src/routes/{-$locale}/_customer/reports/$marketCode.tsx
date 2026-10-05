import { createFileRoute, notFound, useNavigate } from "@tanstack/react-router"
import {
  marketCodeSchema,
  launchMarketCodeSchema,
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
import { twoYearTaipeiRange } from "#/lib/indices"
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
import {
  UsIndexPerformanceTable,
  UsIndexPerformanceTableLoading,
} from "#/components/UsIndexPerformanceTable"
import {
  VixHistoryChart,
  VixHistoryLoading,
} from "#/components/VixHistoryChart"
import {
  MacroDashboard,
  MacroDashboardLoading,
} from "#/components/MacroDashboard"

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
  const isChart = marketCode === "us_equity" || marketCode === "tw_equity"
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
    ready && marketCode === "global_macro_bonds"
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
  const vix = useMarketQuery(
    queries.vix(range),
    locale,
    ready && marketCode === "us_equity"
  )
  const vixAverages = useMarketQuery(
    queries.vixAverages(range),
    locale,
    ready && marketCode === "us_equity"
  )
  const flows = useMarketQuery(
    queries.flows(institutionalFlowRange(range.end)),
    locale,
    ready && marketCode === "tw_equity"
  )
  const stocks = useMarketQuery(
    queries.stocks(range.end),
    locale,
    ready && marketCode === "tw_equity"
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
      {marketCode === "global_macro_bonds" ? (
        <MarketQuerySection query={macro} loading={<MacroDashboardLoading />}>
          {data => <MacroDashboard data={data ?? null} locale={locale} />}
        </MarketQuerySection>
      ) : (
        <div
          className={
            marketCode === "us_equity"
              ? "grid min-w-0 gap-4 xl:grid-cols-2"
              : "min-w-0"
          }
        >
          {marketCode === "us_equity" ? (
            <div className="flex min-w-0 flex-col">
              <MarketQuerySection
                query={history}
                loading={<UsIndexPerformanceTableLoading />}
              >
                {data => (
                  <UsIndexPerformanceTable
                    history={data ?? null}
                    locale={locale}
                  />
                )}
              </MarketQuerySection>
            </div>
          ) : null}
          <div className="min-w-0">
            <MarketQuerySection
              query={report}
              loading={<ReportLoadingScreen />}
            >
              {data =>
                data && data.kind !== "not-found" ? (
                  <ReportInformation locale={locale} report={data} />
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
              marketCode === "tw_equity" ? (
                <TaiwanIndexHistoryLoading />
              ) : (
                <IndexHistoryLoading />
              )
            }
          >
            {data =>
              marketCode === "tw_equity" ? (
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
                />
              )
            }
          </MarketQuerySection>
          <MarketQuerySection query={averages} loading={null}>
            {() => null}
          </MarketQuerySection>
        </>
      ) : null}
      {marketCode === "us_equity" ? (
        <MarketQuerySection query={vix} loading={<VixHistoryLoading />}>
          {data => (
            <VixHistoryChart
              history={
                data ? { ...data, indicators: vixAverages.data ?? null } : null
              }
              locale={locale}
            />
          )}
        </MarketQuerySection>
      ) : null}
      {marketCode === "us_equity" ? (
        <MarketQuerySection query={vixAverages} loading={null}>
          {() => null}
        </MarketQuerySection>
      ) : null}
      {marketCode === "tw_equity" ? (
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
    </>
  )
}
