import { Outlet, createFileRoute, useParams } from "@tanstack/react-router"
import { ReportShell } from "#/components/Reports"
import { marketCodes, type MarketCode } from "#/lib/provisional-reports"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports"
)({
  component: ReportsLayout,
})

function isMarketCode(code: string | undefined): code is MarketCode {
  return code !== undefined && (marketCodes as readonly string[]).includes(code)
}

function ReportsLayout() {
  const { locale } = Route.useRouteContext()
  const { marketCode } = useParams({ strict: false })
  return (
    <ReportShell
      locale={locale}
      activeMarket={isMarketCode(marketCode) ? marketCode : undefined}
    >
      <Outlet />
    </ReportShell>
  )
}
