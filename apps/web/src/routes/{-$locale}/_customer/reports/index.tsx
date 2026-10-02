import type { Locale, NewsroomEdition } from "@daily-insights/api-client"
import { createFileRoute, useLoaderData } from "@tanstack/react-router"
import {
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
} from "#/components/Reports"
import { getReportList } from "#/lib/reports"
import { DailyNews, DailyNewsLoading } from "#/components/DailyNews"
import { getNewsroomEdition } from "#/lib/newsroom"
import { getTodayAnalystViewpoints } from "#/lib/analyst-viewpoints"
import { useChatPageContext } from "#/components/PageContextChat"

export const Route = createFileRoute("/{-$locale}/_customer/reports/")({
  loader: loadReportsAndNews,
  pendingComponent: ReportsAndNewsLoading,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportsPage,
})

type ReportsAndNews = {
  reports: Awaited<ReturnType<typeof getReportList>>
  news: NewsroomEdition | null
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
    getNewsroomEdition({
      data: { locale: context.locale, marketCode: "global" },
    }),
    getTodayAnalystViewpoints(),
  ])
  if (reports.status === "rejected") throw reports.reason
  return {
    reports: reports.value,
    news: news.status === "fulfilled" ? news.value : null,
    viewpoints: viewpoints.status === "fulfilled" ? viewpoints.value : [],
  }
}

function ReportsAndNewsLoading() {
  return (
    <>
      <DailyNewsLoading />
      <ReportLoadingScreen />
    </>
  )
}
// Chat grounds home-page answers in the listed reports and the newsroom
// edition shown above them; without any report there is no page context.
export function reportsIndexChatContext({
  reports,
  news,
}: {
  reports: ReadonlyArray<{ publicationId?: string }>
  news: Pick<NewsroomEdition, "edition_id"> | null
}) {
  const publicationIds = reports.flatMap(report =>
    report.publicationId ? [report.publicationId] : []
  )
  if (!publicationIds.length) return null
  return {
    kind: "reports_index" as const,
    publication_ids: publicationIds,
    news_edition_id: news?.edition_id ?? null,
  }
}

function ReportsPage() {
  const { reports, news, viewpoints } = Route.useLoaderData()
  const markets = useLoaderData({
    from: "/{-$locale}/_customer/reports",
  })
  useChatPageContext(reportsIndexChatContext({ reports, news }))
  return (
    <>
      <DailyNews edition={news} />
      <ReportList viewpoints={viewpoints} markets={markets} />
    </>
  )
}
