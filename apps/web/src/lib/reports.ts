import {
  ApiError,
  createReportClient,
  launchMarketCodeSchema,
  localeSchema,
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
  getTaiwanPreviewReport,
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
const blockTitleKeys: Record<string, string> = {
  "macro.commodities": "reportBlockMacroSnapshot",
  "macro.commodity_normalized_performance":
    "reportBlockMacroCommodityNormalizedPerformance",
  "crypto.overview": "reportBlockCryptoOverview",
  "crypto.normalized_performance": "reportBlockNormalizedPerformance",
  "us.market_movers": "reportBlockUsLeaders",
}
const metricLabelKeys: Record<string, string> = {
  brent: "reportLabelBrent",
  gold: "reportLabelGold",
  copper: "reportLabelCopper",
}
const columnLabelKeys: Record<string, string> = {
  asset: "reportColumnAsset",
  instrument: "reportColumnInstrument",
  price: "reportColumnPrice",
  change: "reportColumnChange",
}

function serverReportClient() {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) throw new Error("API_INTERNAL_URL is required by the web server")
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createReportClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  )
}

function mapBlock(
  block: ApiReportBlock,
  presentationLabel?: ApiReportDetail["presentation"]["labels"][string]
): ReportBlock {
  const titleKey = blockTitleKeys[block.id] ?? "reportsTitle"
  if (block.kind === "metric") {
    return {
      kind: "metric",
      status: block.status,
      titleKey,
      metrics: block.metrics.map(metric => ({
        labelKey: metricLabelKeys[metric.id] ?? metric.id,
        value: metric.value === null ? null : literal(metric.value),
        change: metric.change === null ? null : literal(metric.change),
      })),
    }
  }
  if (block.kind === "table") {
    return {
      kind: "table",
      status: block.status,
      titleKey,
      columns: block.columns.map(
        column => columnLabelKeys[column.id] ?? column.id
      ),
      rows: block.rows.map(row =>
        row.map(cell => {
          if (cell === null) return null
          return literal(cell.text ?? cell.value ?? "")
        })
      ),
    }
  }
  return {
    kind: "series",
    id: block.id,
    status: block.status,
    titleKey,
    title: literal(presentationLabel?.title ?? titleKey),
    unitCode: block.unit_code,
    unitLabel:
      presentationLabel?.unit_label === null ||
      presentationLabel?.unit_label === undefined
        ? null
        : literal(presentationLabel.unit_label),
    sourceDate: block.source_as_of,
    caveat: block.caveat === null ? null : literal(block.caveat),
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
    caveatKey: "reportCaveatLive",
    summaryKey: `reportSummary_${report.market_code}`,
    blocks: [],
  }
}

export function mapReportDetail(report: ApiReportDetail): ProvisionalReport {
  return {
    ...summary(report),
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
    const preview = getTaiwanPreviewReport(data.marketCode)
    if (preview) return { kind: "report" as const, report: preview }
    const parsed = launchMarketCodeSchema.safeParse(data.marketCode)
    if (!parsed.success) return { kind: "not-found" as const }
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
