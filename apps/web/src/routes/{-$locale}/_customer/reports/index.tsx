import { createFileRoute } from "@tanstack/react-router"
import {
  ReportErrorScreen,
  ReportList,
  AnalystViewpointsLoading,
} from "#/components/Reports"
import { DailyNews, DailyNewsLoading } from "#/components/DailyNews"
import { useChatPageContext } from "#/components/PageContextChat"
import { marketQueries, useMarketQuery } from "#/lib/market-queries"
import { MarketQuerySection } from "#/components/MarketQuerySection"

export const Route = createFileRoute("/{-$locale}/_customer/reports/")({
  errorComponent: ReportErrorScreen,
  component: ReportsPage,
})
function ReportsPage() {
  const { user, locale } = Route.useRouteContext()
  const queries = marketQueries(user, locale)
  const markets = useMarketQuery(queries.markets(), locale)
  const reports = useMarketQuery(queries.reports(), locale)
  const news = useMarketQuery(queries.news(), locale)
  const viewpoints = useMarketQuery(queries.viewpoints(), locale)
  const publicationIds = (reports.data ?? []).flatMap(report =>
    report.publicationId ? [report.publicationId] : []
  )
  useChatPageContext(
    publicationIds.length || news.data?.edition_id
      ? {
          kind: "reports_index",
          publication_ids: publicationIds,
          news_edition_id: news.data?.edition_id ?? null,
        }
      : null
  )
  return (
    <>
      <MarketQuerySection query={news} loading={<DailyNewsLoading />}>
        {data => <DailyNews news={data ?? null} />}
      </MarketQuerySection>
      <MarketQuerySection query={reports} loading={null}>
        {() => null}
      </MarketQuerySection>
      <MarketQuerySection
        query={viewpoints}
        loading={<AnalystViewpointsLoading />}
      >
        {data => (
          <ReportList
            viewpoints={data ?? []}
            {...(markets.data ? { markets: markets.data } : {})}
          />
        )}
      </MarketQuerySection>
    </>
  )
}
