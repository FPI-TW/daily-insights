import type { Locale } from "@daily-insights/api-client"
import type { ReactNode } from "react"
import type { MarketCode, ProvisionalReport } from "#/lib/provisional-reports"
import {
  ReportDetail,
  ReportNotGeneratedScreen,
  ReportNotLaunchedScreen,
} from "./Reports"

export type MarketReportState =
  | { kind: "report"; report: ProvisionalReport }
  | { kind: "not-generated"; marketCode: MarketCode }
  | { kind: "not-launched"; marketCode: MarketCode }

export function ReportInformation({
  locale,
  report,
  leadingBlock,
  hideNotLaunched = false,
}: {
  locale: Locale
  report: MarketReportState
  leadingBlock?: ReactNode
  /** The route sets this for markets that already render their own content
   * (index cards, charts), where the notice would only push it down. */
  hideNotLaunched?: boolean
}) {
  if (report.kind === "not-generated")
    return (
      <ReportNotGeneratedScreen
        locale={locale}
        marketCode={report.marketCode}
      />
    )
  if (report.kind === "not-launched")
    return hideNotLaunched ? null : (
      <ReportNotLaunchedScreen locale={locale} marketCode={report.marketCode} />
    )
  return (
    <ReportDetail
      locale={locale}
      report={report.report}
      leadingBlock={leadingBlock}
    />
  )
}
