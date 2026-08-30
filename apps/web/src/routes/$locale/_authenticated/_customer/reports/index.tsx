import { createFileRoute } from "@tanstack/react-router"
import {
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
} from "#/components/Reports"
import { getReportList } from "#/lib/reports"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/"
)({
  loader: ({ context }) => getReportList({ data: context.locale }),
  pendingComponent: ReportLoadingScreen,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportsPage,
})
function ReportsPage() {
  const reports = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  return <ReportList locale={locale} reports={reports} />
}
