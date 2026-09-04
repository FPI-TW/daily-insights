import {
  ApiError,
  createMarketClient,
  createReportClient,
  launchMarketCodeSchema,
  localeSchema,
  marketCodeSchema,
  type ReportBlock as ApiReportBlock,
  type ReportDetail as ApiReportDetail,
  type ReportSummary as ApiReportSummary,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"
import {
  type ProvisionalReport,
  type ReportBlock,
  type ReportValue,
} from "./provisional-reports"

const detailInputSchema = z.object({
  marketCode: z.string(),
  locale: localeSchema,
})
const reportNotGeneratedDetailSchema = z.object({
  code: z.literal("report_not_generated"),
})
const literal = (value: string | number): ReportValue => ({
  kind: "literal",
  value,
})
// API numbers stay Decimal strings until lib/format renders them.
const number = (value: string): ReportValue => ({ kind: "number", value })
const blockTitleKeys: Record<string, string> = {
  "macro.commodities": "reportBlockMacroSnapshot",
  "macro.rates_fx": "reportBlockMacroRatesFx",
  "macro.commodity_normalized_performance":
    "reportBlockMacroCommodityNormalizedPerformance",
  "crypto.overview": "reportBlockCryptoOverview",
  "crypto.normalized_performance": "reportBlockNormalizedPerformance",
  "us.index_proxies": "reportBlockUsIndices",
  "us.mega_caps": "reportBlockUsMegaCaps",
}
const metricLabelKeys: Record<string, string> = {
  brent: "reportLabelBrent",
  gold: "reportLabelGold",
  copper: "reportLabelCopper",
  tlt: "reportLabelTlt",
  ief: "reportLabelIef",
  uup: "reportLabelUup",
  usd_twd: "reportLabelUsdTwd",
  usd_jpy: "reportLabelUsdJpy",
  eur_usd: "reportLabelEurUsd",
  spy: "reportLabelSpy",
  qqq: "reportLabelQqq",
  dia: "reportLabelDia",
  iwm: "reportLabelIwm",
  vixy: "reportLabelVixy",
}
const columnLabelKeys: Record<string, string> = {
  asset: "reportColumnAsset",
  instrument: "reportColumnInstrument",
  price: "reportColumnPrice",
  change: "reportColumnChange",
}

function serverTransport() {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) throw new Error("API_INTERNAL_URL is required by the web server")
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createServerTransport(apiUrl, {
    ...(cookie ? { cookie } : {}),
    ...(requestId ? { requestId } : {}),
  })
}

function serverReportClient() {
  return createReportClient(serverTransport())
}

function mapBlock(
  block: ApiReportBlock,
  presentationLabel?: ApiReportDetail["presentation"]["labels"][string]
): ReportBlock {
  const titleKey = blockTitleKeys[block.id] ?? "reportsTitle"
  const meta = {
    status: block.status,
    titleKey,
    sourceDate: block.source_as_of,
    caveat: block.caveat === null ? null : literal(block.caveat),
  }
  if (block.kind === "metric") {
    return {
      kind: "metric",
      ...meta,
      metrics: block.metrics.map(metric => ({
        labelKey: metricLabelKeys[metric.id] ?? metric.id,
        value: metric.value === null ? null : number(metric.value),
        change: metric.change === null ? null : number(metric.change),
        unitCode: metric.unit_code,
      })),
    }
  }
  if (block.kind === "table") {
    return {
      kind: "table",
      ...meta,
      columns: block.columns.map(column => ({
        labelKey: columnLabelKeys[column.id] ?? column.id,
        unitCode: column.unit_code,
      })),
      rows: block.rows.map(row =>
        row.map(cell => {
          if (cell === null) return null
          if (cell.text !== null) return literal(cell.text)
          return cell.value === null ? null : number(cell.value)
        })
      ),
    }
  }
  return {
    kind: "series",
    id: block.id,
    ...meta,
    title: literal(presentationLabel?.title ?? titleKey),
    unitCode: block.unit_code,
    unitLabel:
      presentationLabel?.unit_label === null ||
      presentationLabel?.unit_label === undefined
        ? null
        : literal(presentationLabel.unit_label),
    series: block.series.map(line => ({
      id: line.id,
      label: literal(
        presentationLabel?.series_labels[line.id] ?? line.id.toUpperCase()
      ),
      points: line.points.map(point => ({
        label: literal(point.x),
        value: point.value === null ? null : Number(point.value),
      })),
    })),
  }
}

function summary(report: ApiReportSummary): ProvisionalReport {
  return {
    publicationId: report.publication_id,
    marketCode: report.market_code,
    status: report.status,
    editionDate: report.edition_date,
    sourceDate: report.source_as_of,
    stale: report.stale,
    staleReason: report.stale_reason,
    caveatKey: "reportCaveatLive",
    summaryKey: `reportSummary_${report.market_code}`,
    blocks: [],
  }
}

export function mapReportDetail(report: ApiReportDetail): ProvisionalReport {
  return {
    ...summary(report),
    caveat: report.content.caveat,
    blocks: report.content.blocks.map(block =>
      mapBlock(block, report.presentation.labels[block.id])
    ),
  }
}

export const getReportList = createServerFn({ method: "GET" })
  .validator(localeSchema)
  .handler(async ({ data: locale }) => {
    setResponseHeader("Cache-Control", "no-store")
    try {
      return (await serverReportClient().list(locale)).map(summary)
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) return []
      throw error
    }
  })

export const getReportDetail = createServerFn({ method: "GET" })
  .validator(detailInputSchema)
  .handler(async ({ data }) => {
    setResponseHeader("Cache-Control", "no-store")
    const parsed = launchMarketCodeSchema.safeParse(data.marketCode)
    if (!parsed.success) {
      // A catalog market without a launched report shows the not-launched
      // state when the organization may see it; anything else is a 404.
      const catalog = marketCodeSchema.safeParse(data.marketCode)
      if (!catalog.success) return { kind: "not-found" as const }
      try {
        const visible = await createMarketClient(serverTransport()).list()
        if (!visible.some(m => m.code === catalog.data && m.is_visible)) {
          return { kind: "not-found" as const }
        }
      } catch (error) {
        // Internal users (no organization) may preview every market.
        if (!(error instanceof ApiError && error.status === 403)) throw error
      }
      return { kind: "not-launched" as const, marketCode: catalog.data }
    }
    const marketCode = parsed.data
    try {
      return {
        kind: "report" as const,
        report: mapReportDetail(
          await serverReportClient().latest(marketCode, data.locale)
        ),
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (reportNotGeneratedDetailSchema.safeParse(error.detail).success) {
          return { kind: "not-generated" as const, marketCode }
        }
        return { kind: "not-found" as const }
      }
      throw error
    }
  })
