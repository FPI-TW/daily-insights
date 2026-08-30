import { createFileRoute, notFound } from "@tanstack/react-router"
import {
  ReportDetail,
  ReportErrorScreen,
  ReportLoadingScreen,
} from "#/components/Reports"
import { getReportDetail } from "#/lib/reports"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/$marketCode"
)({
  loader: async ({ params, context }) => {
    const report = await getReportDetail({
      data: { marketCode: params.marketCode, locale: context.locale },
    })
    if (!report) throw notFound()
    return report
  },
  pendingComponent: ReportLoadingScreen,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportPage,
})
function ReportPage() {
  const report = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  return <ReportDetail locale={locale} report={report} />
}
