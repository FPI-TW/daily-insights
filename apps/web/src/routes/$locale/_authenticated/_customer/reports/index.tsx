import { createFileRoute } from "@tanstack/react-router"
import {
  ReportErrorScreen,
  ReportList,
  ReportLoadingScreen,
} from "#/components/Reports"
import { getReportList } from "#/lib/reports"
import { DailyNews, DailyNewsLoading } from "#/components/DailyNews"
import { getLatestNews } from "#/lib/news"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports/"
)({
  loader: ({ context }) =>
    Promise.all([
      getReportList({ data: context.locale }),
      getLatestNews({ data: context.locale }),
    ]),
  pendingComponent: ReportsAndNewsLoading,
  pendingMs: 0,
  errorComponent: ReportErrorScreen,
  component: ReportsPage,
})

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
  const [reports, news] = Route.useLoaderData()
  const { locale } = Route.useRouteContext()
  return (
    <>
      <ReportList locale={locale} reports={reports} />
      <main className="page-shell pt-0">
        <DailyNews news={news} />
      </main>
    </>
  )
}
