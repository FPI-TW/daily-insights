import { Outlet, createFileRoute, useParams } from "@tanstack/react-router"
import { ReportErrorScreen, ReportShell } from "#/components/Reports"
import { getVisibleMarkets } from "#/lib/markets"
import { marketCodes, type MarketCode } from "#/lib/provisional-reports"

export const Route = createFileRoute(
  "/$locale/_authenticated/_customer/reports"
)({
  loader: ({ context }) => getVisibleMarkets({ data: context.locale }),
  errorComponent: ReportErrorScreen,
  component: ReportsLayout,
})

function isMarketCode(code: string | undefined): code is MarketCode {
  return code !== undefined && (marketCodes as readonly string[]).includes(code)
}

function ReportsLayout() {
  const { locale } = Route.useRouteContext()
  const markets = Route.useLoaderData()
  const { marketCode } = useParams({ strict: false })
  return (
    <ReportShell
      locale={locale}
      markets={markets}
      activeMarket={isMarketCode(marketCode) ? marketCode : undefined}
    >
      <Outlet />
    </ReportShell>
  )
}
