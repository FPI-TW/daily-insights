import type {
  ReportBlock as ApiReportBlock,
  ReportDetail as ApiReportDetail,
  ReportSummary as ApiReportSummary,
} from "@daily-insights/api-client"
import {
  type ProvisionalReport,
  type ReportBlock,
  type ReportValue,
} from "./provisional-reports"

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
  "macro.commodity_ratios": "reportBlockMacroCommodityRatios",
  "crypto.overview": "reportBlockCryptoOverview",
  "crypto.normalized_performance": "reportBlockNormalizedPerformance",
  "us.mega_caps": "reportBlockUsMegaCaps",
}
const metricLabelKeys: Record<string, string> = {
  wti: "reportLabelWti",
  brent: "reportLabelBrent",
  gold: "reportLabelGold",
  silver: "reportLabelSilver",
  copper: "reportLabelCopper",
  tlt: "reportLabelTlt",
  ief: "reportLabelIef",
  uup: "reportLabelUup",
  usd_twd: "reportLabelUsdTwd",
  usd_jpy: "reportLabelUsdJpy",
  eur_usd: "reportLabelEurUsd",
}
const columnLabelKeys: Record<string, string> = {
  asset: "reportColumnAsset",
  instrument: "reportColumnInstrument",
  price: "reportColumnPrice",
  change: "reportColumnChange",
}

function mapBlock(
  block: ApiReportBlock,
  presentationLabel?: ApiReportDetail["presentation"]["labels"][string]
): ReportBlock {
  const titleKey = blockTitleKeys[block.id] ?? "reportsTitle"
  const meta = {
    id: block.id,
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

export function mapReportSummary(report: ApiReportSummary): ProvisionalReport {
  return {
    publicationId: report.publication_id,
    marketCode: report.market_code,
    status: report.status,
    editionDate: report.edition_date,
    sourceDate: report.source_as_of,
    // The API's stale flag and reason code are operator signals; readers
    // never see them, so they are not carried into the view model.
    caveatKey: "reportCaveatLive",
    summaryKey: `reportSummary_${report.market_code}`,
    blocks: [],
  }
}

export function mapReportDetail(report: ApiReportDetail): ProvisionalReport {
  return {
    ...mapReportSummary(report),
    caveat: report.content.caveat,
    blocks: report.content.blocks.flatMap(block =>
      blockTitleKeys[block.id] === undefined
        ? []
        : [mapBlock(block, report.presentation.labels[block.id])]
    ),
  }
}
