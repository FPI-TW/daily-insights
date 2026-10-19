import { Outlet, createFileRoute, useParams } from "@tanstack/react-router"
import { ReportErrorScreen, ReportShell } from "#/components/Reports"
import { marketCodes, type MarketCode } from "#/lib/provisional-reports"
import { marketQueries, useMarketQuery } from "#/lib/market-queries"
import { MarketQuerySection } from "#/components/MarketQuerySection"

export const Route = createFileRoute("/{-$locale}/_customer/reports")({
  errorComponent: ReportErrorScreen,
  component: ReportsLayout,
})
function isMarketCode(code: string | undefined): code is MarketCode {
  return code !== undefined && (marketCodes as readonly string[]).includes(code)
}
function ReportsLayout() {
  const { locale, user } = Route.useRouteContext()
  const markets = useMarketQuery(marketQueries(user, locale).markets(), locale)
  const { marketCode } = useParams({ strict: false })
  return (
    <ReportShell
      locale={locale}
      markets={markets.data ?? []}
      navigationPending={markets.data === undefined && markets.isPending}
      activeMarket={isMarketCode(marketCode) ? marketCode : undefined}
    >
      <MarketQuerySection query={markets} loading={null}>
        {() => null}
      </MarketQuerySection>
      <Outlet />
    </ReportShell>
  )
}
