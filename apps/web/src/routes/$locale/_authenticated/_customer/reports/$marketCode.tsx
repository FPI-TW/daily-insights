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
import {
  type PageContext,
  useChatPageContext,
} from "#/components/PageContextChat"

type ReportResult = Awaited<ReturnType<typeof getReportDetail>>
type MarketPage = {
  report: Exclude<ReportResult, { kind: "not-found" }>
  news: { marketCode: NewsMarketCode; latest: LatestNews | null } | null
}

export function marketPageChatContext({ report }: MarketPage): PageContext {
  if (report.kind === "report" && report.report.publicationId) {
    return {
      kind: "report_detail",
      publication_id: report.report.publicationId,
    }
  }
  return { kind: "global" }
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
  errorComponent: ReportErrorScreen,
  component: ReportPage,
})

function ReportPage() {
  const { report, news } = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  useChatPageContext(marketPageChatContext({ report, news }))
  return (
    <>
      {report.kind === "not-generated" ? (
        <ReportNotGeneratedScreen />
      ) : report.kind === "not-launched" ? (
        <ReportNotLaunchedScreen />
      ) : (
        <ReportDetail locale={locale} report={report.report} />
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
