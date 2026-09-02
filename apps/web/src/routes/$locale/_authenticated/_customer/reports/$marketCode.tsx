import type { LatestNews, Locale } from "@daily-insights/api-client"
import { createFileRoute, notFound } from "@tanstack/react-router"
import { DailyNews } from "#/components/DailyNews"
import {
  ReportDetail,
  ReportErrorScreen,
  ReportLoadingScreen,
  ReportNotGeneratedScreen,
  ReportNotLaunchedScreen,
} from "#/components/Reports"
import { getMarketNews } from "#/lib/news"
import {
  isNewsMarketCode,
  type NewsMarketCode,
} from "#/lib/provisional-reports"
import { getReportDetail } from "#/lib/reports"
import { useChatPageContext } from "#/components/PageContextChat"

type ReportResult = Awaited<ReturnType<typeof getReportDetail>>
type MarketPage = {
  report: Exclude<ReportResult, { kind: "not-found" }>
  news: { marketCode: NewsMarketCode; latest: LatestNews | null } | null
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
  const newsMarket = isNewsMarketCode(params.marketCode)
    ? params.marketCode
    : null
  const [report, news] = await Promise.allSettled([
    getReportDetail({
      data: { marketCode: params.marketCode, locale: context.locale },
    }),
    newsMarket
      ? getMarketNews({
          data: { locale: context.locale, marketCode: newsMarket },
        })
      : Promise.resolve(null),
  ])
  if (report.status === "rejected") throw report.reason
  if (report.value.kind === "not-found") throw notFound()
  return {
    report: report.value,
    news: newsMarket
      ? {
          marketCode: newsMarket,
          latest: news.status === "fulfilled" ? news.value : null,
        }
      : null,
  }
}

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/$marketCode"
)({
  loader: loadMarketPage,
  pendingComponent: ReportLoadingScreen,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportPage,
})

function ReportPage() {
  const { report, news } = Route.useLoaderData()
  useChatPageContext(
    report.kind === "report" && report.report.publicationId
      ? { kind: "report_detail", publication_id: report.report.publicationId }
      : null
  )
  return (
    <>
      {report.kind === "not-generated" ? (
        <ReportNotGeneratedScreen />
      ) : report.kind === "not-launched" ? (
        <ReportNotLaunchedScreen />
      ) : (
        <ReportDetail report={report.report} />
      )}
      {news ? (
        <DailyNews
          news={news.latest}
          eyebrowKey="marketNewsEyebrow"
          titleKey={`marketNewsTitle_${news.marketCode}`}
        />
      ) : null}
    </>
  )
}
