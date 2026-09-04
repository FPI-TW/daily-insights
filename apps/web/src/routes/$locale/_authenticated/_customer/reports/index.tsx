import type { LatestNews, Locale } from "@daily-insights/api-client"
import { createFileRoute, useLoaderData } from "@tanstack/react-router"
import {
  type OverviewEntry,
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
  ReportOverview,
} from "#/components/Reports"
import { getReportDetail, getReportList } from "#/lib/reports"
import { DailyNews, DailyNewsLoading } from "#/components/DailyNews"
import { getLatestNews } from "#/lib/news"
import { getTodayAnalystViewpoints } from "#/lib/analyst-viewpoints"
import { useChatPageContext } from "#/components/PageContextChat"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/"
)({
  loader: loadReportsAndNews,
  pendingComponent: ReportsAndNewsLoading,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportsPage,
})

type ReportsAndNews = {
  reports: Awaited<ReturnType<typeof getReportList>>
  overview: OverviewEntry[]
  news: LatestNews | null
  viewpoints: Awaited<ReturnType<typeof getTodayAnalystViewpoints>>
}

// The news panel is secondary: a news API failure must not replace the
// report list with the route error screen, so only the report request is
// allowed to reject and news degrades to its unavailable state.
export async function loadReportsAndNews({
  context,
}: {
  context: { locale: Locale }
}): Promise<ReportsAndNews> {
  const [reports, news, viewpoints] = await Promise.allSettled([
    getReportList({ data: context.locale }),
    getLatestNews({ data: context.locale }),
    getTodayAnalystViewpoints(),
  ])
  if (reports.status === "rejected") throw reports.reason
  // Headline figures for the overview cards come from each report's detail;
  // a failed detail only empties that card.
  const details = await Promise.allSettled(
    reports.value.map(report =>
      getReportDetail({
        data: { marketCode: report.marketCode, locale: context.locale },
      })
    )
  )
  const overview = reports.value.map((summary, index) => {
    const detail = details[index]
    return {
      summary,
      detail:
        detail?.status === "fulfilled" && detail.value.kind === "report"
          ? detail.value.report
          : null,
    }
  })
  return {
    reports: reports.value,
    overview,
    news: news.status === "fulfilled" ? news.value : null,
    viewpoints: viewpoints.status === "fulfilled" ? viewpoints.value : [],
  }
}

function ReportsAndNewsLoading() {
  return (
    <>
      <ReportLoadingScreen />
      <main className="page-shell pt-0">
        <DailyNewsLoading />
      </main>
    </>
  )
}
function ReportsPage() {
  const { reports, overview, news, viewpoints } = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  const markets = useLoaderData({
    from: "/$locale/_authenticated/_customer/reports",
  })
  const publicationIds = reports.flatMap(report =>
    report.publicationId ? [report.publicationId] : []
  )
  useChatPageContext(
    publicationIds.length
      ? {
          kind: "reports_index",
          publication_ids: publicationIds,
          news_edition_id: news?.edition_id ?? null,
        }
      : null
  )
  return (
    <>
      <ReportOverview locale={locale} entries={overview} />
      <ReportList viewpoints={viewpoints} markets={markets} />
      <DailyNews news={news} />
    </>
  )
}
