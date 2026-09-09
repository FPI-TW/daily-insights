import { marketCodeSchema } from "@daily-insights/api-client"

// Markets with a launched morning report; everything else in the catalog shows
// the not-launched state (plus market news where an edition exists).
export const launchMarketCodes = [
  "global_macro_bonds",
  "crypto",
  "us_equity",
] as const
// Markets that publish their own daily news edition.
export const newsMarketCodes = ["tw_equity", "us_equity"] as const
export const marketCodes = marketCodeSchema.options

export type MarketCode = (typeof marketCodes)[number]
export type NewsMarketCode = (typeof newsMarketCodes)[number]
export function isNewsMarketCode(code: string): code is NewsMarketCode {
  return (newsMarketCodes as readonly string[]).includes(code)
}
export type ReportStatus = "complete" | "partial" | "unavailable"
export type BlockStatus = "ok" | "missing" | "error"
// `number` carries an API Decimal string that must go through lib/format;
// `literal` is already display text and `translation` is an i18n key.
export type ReportValue =
  | { kind: "literal"; value: string | number }
  | { kind: "number"; value: string }
  | { kind: "translation"; key: string }

type Metric = {
  labelKey: string
  value: ReportValue | null
  change?: ReportValue | null
  unitCode?: string | null
}
type BlockMeta = {
  /** Source block identity. Optional only for legacy/UI fixtures. */
  id?: string
  status: BlockStatus
  titleKey: string
  captionKey?: string
  sourceDate?: string | null
  caveat?: ReportValue | null
}
export type MetricBlock = BlockMeta & {
  kind: "metric"
  metrics: ReadonlyArray<Metric>
}
export type TableColumn = { labelKey: string; unitCode: string | null }
export type TableBlock = BlockMeta & {
  kind: "table"
  columns: ReadonlyArray<TableColumn>
  rows: ReadonlyArray<ReadonlyArray<ReportValue | null>>
}
export type SeriesBlock = BlockMeta & {
  kind: "series"
  id?: string
  title?: ReportValue
  caption?: ReportValue | null
  unitCode?: string
  unitLabel?: ReportValue | null
  series: ReadonlyArray<{
    id: string
    label: ReportValue
    points: ReadonlyArray<{ label: ReportValue; value: number | null }>
  }>
}
export type ReportBlock = MetricBlock | TableBlock | SeriesBlock
export type ProvisionalReport = {
  publicationId?: string
  marketCode: MarketCode
  status: ReportStatus
  editionDate: string
  sourceDate: string | null
  caveat?: string | null
  caveatKey: string
  summaryKey: string
  blocks: ReadonlyArray<ReportBlock>
  preview?: boolean
}
