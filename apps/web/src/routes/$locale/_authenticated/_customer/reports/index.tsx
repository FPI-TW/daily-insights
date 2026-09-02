import type { LatestNews, Locale } from "@daily-insights/api-client"
import { createFileRoute } from "@tanstack/react-router"
import {
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
} from "#/components/Reports"
import { getReportList } from "#/lib/reports"
import { DailyNews, DailyNewsLoading } from "#/components/DailyNews"
import { getLatestNews } from "#/lib/news"
import { useChatPageContext } from "#/components/PageContextChat"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/"
)({
  loader: loadReportsAndNews,
  pendingComponent: ReportsAndNewsLoading,
  errorComponent: ReportErrorScreen,
  component: ReportsPage,
})

type ReportsAndNews = {
  reports: Awaited<ReturnType<typeof getReportList>>
  news: LatestNews | null
}

// The news panel is secondary: a news API failure must not replace the
// report list with the route error screen, so only the report request is
// allowed to reject and news degrades to its unavailable state.
export async function loadReportsAndNews({
  context,
}: {
  context: { locale: Locale }
}): Promise<ReportsAndNews> {
  const [reports, news] = await Promise.allSettled([
    getReportList({ data: context.locale }),
    getLatestNews({ data: context.locale }),
  ])
  if (reports.status === "rejected") throw reports.reason
  return {
    reports: reports.value,
    news: news.status === "fulfilled" ? news.value : null,
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
  const { reports, news } = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
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
      <ReportList locale={locale} reports={reports} />
      <main className="page-shell pt-0">
        <DailyNews news={news} />
      </main>
    </>
  )
}
