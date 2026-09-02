export const launchMarketCodes = [
  "global_macro_bonds",
  "crypto",
  "us_equity",
] as const
// Markets with a report page in navigation. Taiwan equities has no launched
// report yet; its page shows the not-launched state plus Taiwan market news.
export const navMarketCodes = [...launchMarketCodes, "tw_equity"] as const
// Markets that publish their own daily news edition.
export const newsMarketCodes = ["tw_equity", "us_equity"] as const
export const marketCodes = navMarketCodes

export type MarketCode = (typeof marketCodes)[number]
export type NewsMarketCode = (typeof newsMarketCodes)[number]
export function isNewsMarketCode(code: string): code is NewsMarketCode {
  return (newsMarketCodes as readonly string[]).includes(code)
}
export type ReportStatus = "complete" | "partial" | "unavailable"
export type BlockStatus = "ok" | "missing" | "error"
export type ReportValue =
  | { kind: "literal"; value: string | number }
  | { kind: "translation"; key: string }

type Metric = {
  labelKey: string
  value: ReportValue | null
  change?: ReportValue | null
}
export type MetricBlock = {
  kind: "metric"
  status: BlockStatus
  titleKey: string
  captionKey?: string
  metrics: ReadonlyArray<Metric>
}
export type TableBlock = {
  kind: "table"
  status: BlockStatus
  titleKey: string
  captionKey?: string
  columns: ReadonlyArray<string>
  rows: ReadonlyArray<ReadonlyArray<ReportValue | null>>
}
export type SeriesBlock = {
  kind: "series"
  id?: string
  status: BlockStatus
  titleKey: string
  title?: ReportValue
  captionKey?: string
  caption?: ReportValue | null
  unitCode?: string
  unitLabel?: ReportValue | null
  sourceDate?: string | null
  caveat?: ReportValue | null
  series: ReadonlyArray<{
    id: string
    label: ReportValue
    points: ReadonlyArray<{ label: ReportValue; value: number | null }>
  }>
}
export type ReportBlock = MetricBlock | TableBlock | SeriesBlock
export type ProvisionalReport = {
  marketCode: MarketCode
  status: ReportStatus
  editionDate: string
  sourceDate: string | null
  caveatKey: string
  summaryKey: string
  blocks: ReadonlyArray<ReportBlock>
  preview?: boolean
}
