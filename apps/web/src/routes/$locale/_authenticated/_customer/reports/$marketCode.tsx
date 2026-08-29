import { createFileRoute, notFound } from "@tanstack/react-router"
import {
  ReportDetail,
  ReportErrorScreen,
  ReportLoadingScreen,
} from "#/components/Reports"
import { getProvisionalReport } from "#/lib/provisional-reports"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/$marketCode"
)({
  loader: async ({ params }) => {
    const report = await getProvisionalReport(params.marketCode)
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
