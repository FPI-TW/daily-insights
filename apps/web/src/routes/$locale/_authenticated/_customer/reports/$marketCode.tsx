import { createFileRoute, notFound } from "@tanstack/react-router"
import {
  ReportDetail,
  ReportErrorScreen,
  ReportLoadingScreen,
  ReportNotGeneratedScreen,
} from "#/components/Reports"
import { getReportDetail } from "#/lib/reports"
import { useChatPageContext } from "#/components/PageContextChat"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/$marketCode"
)({
  loader: async ({ params, context }) => {
    const result = await getReportDetail({
      data: { marketCode: params.marketCode, locale: context.locale },
    })
    if (result.kind === "not-found") throw notFound()
    return result
  },
  pendingComponent: ReportLoadingScreen,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportPage,
})
function ReportPage() {
  const result = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  useChatPageContext(
    result.kind === "report" && result.report.publicationId
      ? { kind: "report_detail", publication_id: result.report.publicationId }
      : null
  )
  if (result.kind === "not-generated") {
    return (
      <ReportNotGeneratedScreen
        locale={locale}
        marketCode={result.marketCode}
      />
    )
  }
  return <ReportDetail locale={locale} report={result.report} />
}
