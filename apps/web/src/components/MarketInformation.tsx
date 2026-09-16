import type { Locale } from "@daily-insights/api-client"
import { Await } from "@tanstack/react-router"
import { Suspense, type ReactNode } from "react"
import type { MacroDashboardData } from "#/lib/macro-dashboard"
import type {
  IndexMovingAverageMap,
  MarketIndexHistory,
  VixHistory,
} from "#/lib/indices"
import type { TaiwanInstitutionalData } from "#/lib/institutional-flows"
import type { MarketCode, ProvisionalReport } from "#/lib/provisional-reports"
import { IndexHistoryChart, IndexHistoryLoading } from "./IndexHistoryChart"
import { MacroDashboard, MacroDashboardLoading } from "./MacroDashboard"
import {
  ReportDetail,
  ReportLoadingScreen,
  ReportNotGeneratedScreen,
  ReportNotLaunchedScreen,
} from "./Reports"
import {
  TaiwanIndexHistoryChart,
  TaiwanIndexHistoryLoading,
} from "./TaiwanIndexHistoryChart"
import {
  TaiwanInstitutionalFlows,
  TaiwanInstitutionalFlowsLoading,
} from "./TaiwanInstitutionalFlows"
import {
  UsIndexPerformanceTable,
  UsIndexPerformanceTableLoading,
} from "./UsIndexPerformanceTable"
import { VixHistoryChart, VixHistoryLoading } from "./VixHistoryChart"

type MarketReportState =
  | { kind: "report"; report: ProvisionalReport }
  | { kind: "not-generated"; marketCode: MarketCode }
  | { kind: "not-launched"; marketCode: MarketCode }

function ReportInformation({
  locale,
  report,
  leadingBlock,
}: {
  locale: Locale
  report: MarketReportState
  leadingBlock?: ReactNode
}) {
  if (report.kind === "not-generated") {
    return (
      <ReportNotGeneratedScreen
        locale={locale}
        marketCode={report.marketCode}
      />
    )
  }
  if (report.kind === "not-launched") {
    return (
      <ReportNotLaunchedScreen locale={locale} marketCode={report.marketCode} />
    )
  }
  return (
    <ReportDetail
      locale={locale}
      report={report.report}
      leadingBlock={leadingBlock}
    />
  )
}

export function GlobalMacroMarketInformation({
  dashboard,
  locale,
}: {
  dashboard: Promise<MacroDashboardData | null>
  locale: Locale
}) {
  return (
    <Suspense fallback={<MacroDashboardLoading />}>
      <Await promise={dashboard}>
        {data => <MacroDashboard data={data} locale={locale} />}
      </Await>
    </Suspense>
  )
}

export function CryptoMarketInformation({
  locale,
  report,
}: {
  locale: Locale
  report: MarketReportState
}) {
  return <ReportInformation locale={locale} report={report} />
}

export function UsEquityMarketInformation({
  indexHistory,
  indexMovingAverages,
  locale,
  report,
  vixHistory,
}: {
  indexHistory: Promise<MarketIndexHistory | null> | null
  indexMovingAverages: Promise<IndexMovingAverageMap> | null
  locale: Locale
  report: MarketReportState
  vixHistory: Promise<VixHistory | null> | null
}) {
  return (
    <>
      <ReportInformation
        locale={locale}
        report={report}
        leadingBlock={
          indexHistory ? (
            <Suspense fallback={<UsIndexPerformanceTableLoading />}>
              <Await promise={indexHistory}>
                {history => (
                  <UsIndexPerformanceTable history={history} locale={locale} />
                )}
              </Await>
            </Suspense>
          ) : null
        }
      />
      {indexHistory ? (
        <Suspense fallback={<IndexHistoryLoading />}>
          <Await promise={indexHistory}>
            {history => (
              <IndexHistoryChart
                history={history}
                locale={locale}
                movingAverages={indexMovingAverages}
              />
            )}
          </Await>
        </Suspense>
      ) : null}
      {vixHistory ? (
        <Suspense fallback={<VixHistoryLoading />}>
          <Await promise={vixHistory}>
            {history => <VixHistoryChart history={history} locale={locale} />}
          </Await>
        </Suspense>
      ) : null}
    </>
  )
}

export function TaiwanEquityMarketInformation({
  indexHistory,
  indexMovingAverages,
  institutionalData,
  locale,
  report,
}: {
  indexHistory: Promise<MarketIndexHistory | null> | null
  indexMovingAverages: Promise<IndexMovingAverageMap> | null
  institutionalData: Promise<TaiwanInstitutionalData> | null
  locale: Locale
  report: MarketReportState
}) {
  return (
    <>
      <ReportInformation locale={locale} report={report} />
      {indexHistory ? (
        <Suspense
          fallback={
            <>
              <TaiwanIndexHistoryLoading />
              <TaiwanInstitutionalFlowsLoading />
            </>
          }
        >
          <Await promise={indexHistory}>
            {history => (
              <>
                <TaiwanIndexHistoryChart
                  history={history}
                  locale={locale}
                  movingAverages={indexMovingAverages}
                />
                {institutionalData ? (
                  <Suspense fallback={<TaiwanInstitutionalFlowsLoading />}>
                    <Await promise={institutionalData}>
                      {data => (
                        <TaiwanInstitutionalFlows
                          data={data}
                          history={history}
                          locale={locale}
                        />
                      )}
                    </Await>
                  </Suspense>
                ) : null}
              </>
            )}
          </Await>
        </Suspense>
      ) : null}
    </>
  )
}

export function OtherMarketInformation({
  locale,
  report,
}: {
  locale: Locale
  report: MarketReportState
}) {
  return <ReportInformation locale={locale} report={report} />
}

export function MarketInformationLoading() {
  return <ReportLoadingScreen />
}
