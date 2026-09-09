import { z } from "zod"
import trackedIndexCatalogContract from "./tracked-index-catalog.json"

export const localeSchema = z.enum(["zh-hant", "zh-hans", "en"])
export type Locale = z.infer<typeof localeSchema>

export const launchMarketCodeSchema = z.enum([
  "global_macro_bonds",
  "crypto",
  "us_equity",
])
export type LaunchMarketCode = z.infer<typeof launchMarketCodeSchema>
// Every market in the API catalog; visibility per organization comes from
// /api/markets, which is what drives customer navigation.
export const marketCodeSchema = z.enum([
  "global_macro_bonds",
  "forex",
  "crypto",
  "us_equity",
  "hk_equity",
  "cn_equity",
  "tw_equity",
  "tw_index_derivatives",
])
export type MarketCode = z.infer<typeof marketCodeSchema>
export const marketSchema = z.object({
  code: marketCodeSchema,
  is_visible: z.boolean(),
  name_en: z.string(),
  name_zh_hant: z.string(),
  name_zh_hans: z.string(),
})
export type Market = z.infer<typeof marketSchema>
export const marketListSchema = z.array(marketSchema)
export const reportStatusSchema = z.enum(["complete", "partial", "unavailable"])
export const blockStatusSchema = z.enum(["ok", "missing", "error"])
const decimalSchema = z.string().regex(/^-?\d+(?:\.\d+)?$/)
export const indexSymbolSchema = z.enum([
  "^DJI",
  "^GSPC",
  "^NDX",
  "^RUT",
  "^SOX",
  "^VIX",
  "^HSI",
  "^TWII",
  "000001.SS",
])
export type IndexSymbol = z.infer<typeof indexSymbolSchema>
// This is the reviewed tracked-index catalog, rather than a row-derived
// discovery list. A missing first backfill row must not hide its symbol.
export const trackedIndexCatalog =
  trackedIndexCatalogContract as ReadonlyArray<{
    readonly symbol: IndexSymbol
    readonly marketCode: MarketCode
  }>
export const indexDailyBarSchema = z.object({
  symbol: z.string().min(1),
  market_code: marketCodeSchema,
  trade_date: z.iso.date(),
  open: decimalSchema.nullable(),
  high: decimalSchema.nullable(),
  low: decimalSchema.nullable(),
  close: decimalSchema,
  volume: z.number().int().nonnegative().nullable(),
})
export type IndexDailyBar = z.infer<typeof indexDailyBarSchema>
export const indexDailyBarListSchema = z.array(indexDailyBarSchema)
export const indexMovingAveragePointSchema = z.object({
  trade_date: z.iso.date(),
  value: decimalSchema.nullable(),
})
export type IndexMovingAveragePoint = z.infer<
  typeof indexMovingAveragePointSchema
>
export const indexMovingAverageSeriesSchema = z.object({
  period: z.union([
    z.literal(20),
    z.literal(60),
    z.literal(120),
    z.literal(240),
  ]),
  points: z.array(indexMovingAveragePointSchema),
})
export type IndexMovingAverageSeries = z.infer<
  typeof indexMovingAverageSeriesSchema
>
export const indexMovingAveragesSchema = z.object({
  symbol: z.string().min(1),
  market_code: marketCodeSchema,
  method: z.literal("sma"),
  price_field: z.literal("close"),
  formula_version: z.literal("sma-close-v1"),
  as_of: z.iso.date().nullable(),
  series: z.tuple([
    indexMovingAverageSeriesSchema.extend({ period: z.literal(20) }),
    indexMovingAverageSeriesSchema.extend({ period: z.literal(60) }),
    indexMovingAverageSeriesSchema.extend({ period: z.literal(120) }),
    indexMovingAverageSeriesSchema.extend({ period: z.literal(240) }),
  ]),
})
export type IndexMovingAverages = z.infer<typeof indexMovingAveragesSchema>
export const indexLatestBarSchema = indexDailyBarSchema.extend({
  previous_close: decimalSchema.nullable(),
})
export type IndexLatestBar = z.infer<typeof indexLatestBarSchema>
export const indexLatestBarListSchema = z.array(indexLatestBarSchema)
export const institutionalFlowPointSchema = z.object({
  trade_date: z.iso.date(),
  foreign: decimalSchema,
  trust: decimalSchema,
  dealer: decimalSchema,
  total: decimalSchema,
})
export const institutionalFlowsSchema = z.object({
  as_of: z.iso.date().nullable(),
  contract_version: z.string().min(1),
  contract_hash: z.string().length(64),
  endpoint: z.string().min(1),
  series: z.array(institutionalFlowPointSchema),
})
export type InstitutionalFlows = z.infer<typeof institutionalFlowsSchema>
export const institutionalStockFlowSchema = z.object({
  symbol: z.string().min(1),
  // Copied verbatim from TWSE and never validated on the way in, unlike the
  // symbol. Requiring it here would turn one blank name into a rejected
  // response and an empty panel, which is worse than a row showing its code.
  name: z.string(),
  foreign_lots: decimalSchema,
  trust_lots: decimalSchema,
  dealer_lots: decimalSchema,
  total_lots: decimalSchema,
})
export const institutionalStocksSchema = z.object({
  as_of: z.iso.date().nullable(),
  contract_version: z.string().min(1),
  contract_hash: z.string().length(64),
  endpoint: z.string().min(1),
  rows: z.array(institutionalStockFlowSchema),
})
export type InstitutionalStocks = z.infer<typeof institutionalStocksSchema>
export const yfinanceSymbolBarsSchema = z.object({
  symbol: indexSymbolSchema,
  market: marketCodeSchema,
  as_of: z.iso.date(),
  stored_count: z.number().int().nonnegative(),
  dropped_unsettled_trade_date: z.iso.date().nullable(),
})
export const yfinanceSymbolFailureSchema = z.object({
  symbol: indexSymbolSchema,
  market: marketCodeSchema,
  error: z.string().min(1),
})
export const yfinanceDailyBarsResponseSchema = z.object({
  period: z.literal("7d"),
  fetched_at: z.iso.datetime({ offset: true }),
  succeeded: z.array(yfinanceSymbolBarsSchema),
  failed: z.array(yfinanceSymbolFailureSchema),
})
export type YfinanceDailyBarsResponse = z.infer<
  typeof yfinanceDailyBarsResponseSchema
>
export const dataManagementOperationSchema = z.enum([
  "morning_all",
  "morning_market",
  "index_yahoo",
  "institutional_twse",
  "news_all",
  "news_market",
  "news_publish",
  "macro_dashboard",
])
export const dataManagementRunOperationGroupSchema = z.enum(["news"])
export type DataManagementRunOperationGroup = z.infer<
  typeof dataManagementRunOperationGroupSchema
>
export const dataManagementNewsMarketCodeSchema = z.enum([
  "global",
  "tw_equity",
  "us_equity",
])
export const dataManagementRunStatusSchema = z.enum([
  "pending",
  "running",
  "succeeded",
  "partial",
  "failed",
  "cancelled",
])
export const dataManagementRunCreateSchema = z.discriminatedUnion("operation", [
  z.object({ operation: z.literal("morning_all") }),
  z.object({
    operation: z.literal("morning_market"),
    market_code: launchMarketCodeSchema,
  }),
  z.object({ operation: z.literal("index_yahoo") }),
  z.object({ operation: z.literal("institutional_twse") }),
  z.object({ operation: z.literal("news_all") }),
  z.object({
    operation: z.literal("news_market"),
    market_code: dataManagementNewsMarketCodeSchema,
  }),
  z.object({ operation: z.literal("macro_dashboard") }),
])
export type DataManagementRunCreateInput = z.infer<
  typeof dataManagementRunCreateSchema
>
export const dataManagementCatalogSchema = z.object({
  taipei_date: z.iso.date(),
  morning_reports_enabled: z.boolean(),
  yfinance_enabled: z.boolean(),
  twse_enabled: z.boolean(),
  markets: z.array(launchMarketCodeSchema),
  daily_news_enabled: z.boolean(),
  news_markets: z.array(dataManagementNewsMarketCodeSchema),
  macro_dashboard_enabled: z.boolean(),
})
export type DataManagementCatalog = z.infer<typeof dataManagementCatalogSchema>
const dataManagementRunBaseSchema = z.object({
  id: z.uuid(),
  edition_date: z.iso.date(),
  status: dataManagementRunStatusSchema,
  // Null when the scheduler queued the run rather than an administrator.
  requested_by_user_id: z.uuid().nullable(),
  created_at: z.iso.datetime({ offset: true }),
  started_at: z.iso.datetime({ offset: true }).nullable(),
  completed_at: z.iso.datetime({ offset: true }).nullable(),
  result: z.record(z.string(), z.unknown()).nullable(),
  error: z.string().nullable(),
})
export const dataManagementRunSchema = z.discriminatedUnion("operation", [
  dataManagementRunBaseSchema.extend({
    operation: z.literal("morning_all"),
    market_code: z.null(),
  }),
  dataManagementRunBaseSchema.extend({
    operation: z.literal("macro_dashboard"),
    market_code: z.null(),
  }),
  dataManagementRunBaseSchema.extend({
    operation: z.literal("morning_market"),
    market_code: launchMarketCodeSchema,
  }),
  dataManagementRunBaseSchema.extend({
    operation: z.literal("index_yahoo"),
    market_code: z.null(),
  }),
  dataManagementRunBaseSchema.extend({
    operation: z.literal("institutional_twse"),
    market_code: z.null(),
  }),
  dataManagementRunBaseSchema.extend({
    operation: z.literal("news_all"),
    market_code: z.null(),
  }),
  dataManagementRunBaseSchema.extend({
    operation: z.literal("news_market"),
    market_code: dataManagementNewsMarketCodeSchema,
  }),
  // Manual publication of news candidates; created through the news admin
  // endpoint rather than the generic run form, so the create schema omits it.
  dataManagementRunBaseSchema.extend({
    operation: z.literal("news_publish"),
    market_code: z.null(),
  }),
])
export type DataManagementRun = z.infer<typeof dataManagementRunSchema>
export const dataManagementRunListSchema = z.object({
  items: z.array(dataManagementRunSchema),
})
const chartPointSchema = z.object({
  x: z.string().min(1),
  value: decimalSchema.nullable(),
})
const chartSeriesSchema = z.object({
  id: z.string(),
  points: z.array(chartPointSchema),
})
const metricBlockSchema = z.object({
  id: z.string(),
  kind: z.literal("metric"),
  status: blockStatusSchema,
  source_as_of: z.iso.date().nullable(),
  caveat: z.string().nullable(),
  metrics: z.array(
    z.object({
      id: z.string(),
      value: decimalSchema.nullable(),
      change: decimalSchema.nullable(),
      unit_code: z.string(),
    })
  ),
})
const tableBlockSchema = z.object({
  id: z.string(),
  kind: z.literal("table"),
  status: blockStatusSchema,
  source_as_of: z.iso.date().nullable(),
  caveat: z.string().nullable(),
  columns: z.array(
    z.object({ id: z.string(), unit_code: z.string().nullable() })
  ),
  rows: z.array(
    z.array(
      z
        .object({
          text: z.string().nullable(),
          value: decimalSchema.nullable(),
        })
        .nullable()
    )
  ),
})
const seriesBlockSchema = z.object({
  id: z.string(),
  kind: z.literal("series"),
  status: blockStatusSchema,
  source_as_of: z.iso.date().nullable(),
  caveat: z.string().nullable(),
  unit_code: z.string(),
  series: z.array(chartSeriesSchema),
})
export const reportBlockSchema = z.discriminatedUnion("kind", [
  metricBlockSchema,
  tableBlockSchema,
  seriesBlockSchema,
])
export type ReportBlock = z.infer<typeof reportBlockSchema>
export const reportSummarySchema = z.object({
  publication_id: z.uuid(),
  report_key: z.string(),
  market_code: launchMarketCodeSchema,
  edition_date: z.iso.date(),
  revision: z.number().int().positive(),
  source_as_of: z.iso.date().nullable(),
  published_at: z.iso.datetime({ offset: true }),
  stale: z.boolean(),
  stale_reason: z.string().nullable(),
  status: reportStatusSchema,
  title: z.string(),
  summary: z.string().nullable(),
  locale: localeSchema,
})
export type ReportSummary = z.infer<typeof reportSummarySchema>
export const reportListSchema = z.array(reportSummarySchema)
export const reportDetailSchema = reportSummarySchema.extend({
  manifest_version: z.string(),
  manifest_hash: z.string().length(64),
  content: z.object({
    schema_version: z.string(),
    market_code: launchMarketCodeSchema,
    as_of: z.iso.date().nullable(),
    status: reportStatusSchema,
    caveat: z.string().nullable(),
    blocks: z.array(reportBlockSchema),
    metrics: z.array(z.unknown()),
    charts: z.array(z.unknown()),
  }),
  presentation: z.object({
    schema_version: z.string(),
    locale: localeSchema,
    title: z.string(),
    summary: z.string().nullable(),
    labels: z.record(
      z.string(),
      z.object({
        title: z.string(),
        description: z.string().nullable(),
        unit_label: z.string().nullable(),
        series_labels: z.record(z.string(), z.string()),
      })
    ),
  }),
})
export type ReportDetail = z.infer<typeof reportDetailSchema>

export const analystViewpointSchema = z.object({
  viewpoint_date: z.iso.date(),
  market_code: marketCodeSchema,
  source_market_code: z.enum([
    "us_macro",
    "forex",
    "crypto",
    "us_stocks",
    "hk_stocks",
    "cn_stocks",
    "tw_stocks",
    "tw_futures",
  ]),
  points: z.array(z.string().min(1)),
  fetched_at: z.iso.datetime({ offset: true }),
})
export type AnalystViewpoint = z.infer<typeof analystViewpointSchema>
export const analystViewpointListSchema = z.array(analystViewpointSchema)

export const analystViewpointSyncMarketStatusSchema = z.object({
  source_market_code: analystViewpointSchema.shape.source_market_code,
  market_code: analystViewpointSchema.shape.market_code,
  status: z.enum(["updated", "missing", "stale"]),
})
export const analystViewpointSyncSchema = z.object({
  viewpoint_date: z.iso.date(),
  fetched_at: z.iso.datetime({ offset: true }),
  status: z.enum(["complete", "partial"]),
  markets: z.array(analystViewpointSyncMarketStatusSchema),
})
export type AnalystViewpointSync = z.infer<typeof analystViewpointSyncSchema>
export const analystViewpointExecutionSchema = z.object({
  viewpoint_date: z.iso.date(),
  trigger: z.enum(["scheduler", "manual"]),
  status: z.enum(["complete", "partial", "failed"]),
  fetched_at: z.iso.datetime({ offset: true }).nullable(),
  completed_at: z.iso.datetime({ offset: true }),
  error_code: z.string().nullable(),
  markets: z.array(analystViewpointSyncMarketStatusSchema),
})
export type AnalystViewpointExecution = z.infer<
  typeof analystViewpointExecutionSchema
>
export const analystViewpointSyncStatusSchema = z.object({
  enabled: z.boolean(),
  today: z.iso.date(),
  viewpoints: analystViewpointListSchema,
  latest_sync: analystViewpointExecutionSchema.nullable(),
})
export type AnalystViewpointSyncStatus = z.infer<
  typeof analystViewpointSyncStatusSchema
>

export const newsMarketSchema = z.enum([
  "global",
  "us",
  "asia",
  "china",
  "taiwan",
  "europe",
  "commodities",
  "crypto",
])
export type NewsMarket = z.infer<typeof newsMarketSchema>
export const newsTopicSchema = z.enum([
  "markets",
  "economy",
  "companies",
  "policy",
  "technology",
  "commodities",
])
export type NewsTopic = z.infer<typeof newsTopicSchema>
export const newsItemSchema = z.object({
  id: z.uuid(),
  rank: z.number().int().positive(),
  importance: z.number().int().min(1).max(5),
  topic: newsTopicSchema,
  headline: z.string(),
  summary: z.string(),
  source_name: z.string(),
  source_hostname: z.string(),
  source_url: z.url(),
  source_published_at: z.iso.datetime({ offset: true }).nullable(),
  numeric_facts: z.array(z.string()),
  // Null on editions generated before selection metadata was persisted.
  market: newsMarketSchema.nullable(),
  event_key: z.string().nullable(),
})
export type NewsItem = z.infer<typeof newsItemSchema>
export const newsMarketCodeSchema = z.enum(["tw_equity", "us_equity"])
export type NewsMarketCode = z.infer<typeof newsMarketCodeSchema>

export const latestNewsSchema = z.object({
  market_code: z.string(),
  target_items: z.number().int().positive(),
  edition_id: z.uuid().nullable(),
  edition_date: z.iso.date().nullable(),
  revision: z.number().int().positive().nullable(),
  generated_at: z.iso.datetime({ offset: true }).nullable(),
  status: reportStatusSchema,
  locale: localeSchema,
  caveat: z.string().nullable(),
  items: z.array(newsItemSchema),
})
export type LatestNews = z.infer<typeof latestNewsSchema>

// Admin curation view of an edition: what the pipeline discovered, what the
// model did with it and which stories are published or hidden.
export const newsCandidateStageSchema = z.enum([
  "discovered",
  "fetch_failed",
  "unused",
  "reviewed",
  "dropped",
  "published",
])
export type NewsCandidateStage = z.infer<typeof newsCandidateStageSchema>
export const newsCandidateDropReasonSchema = z.enum([
  "off_market",
  "policy",
  "duplicate_event",
  "summary_failed",
  "reserve",
])
export type NewsCandidateDropReason = z.infer<
  typeof newsCandidateDropReasonSchema
>
export const newsItemOriginSchema = z.enum(["model", "manual"])
export type NewsItemOrigin = z.infer<typeof newsItemOriginSchema>
export const newsAdminItemSchema = z.object({
  id: z.uuid(),
  rank: z.number().int().positive(),
  origin: newsItemOriginSchema,
  hidden: z.boolean(),
  hidden_at: z.iso.datetime({ offset: true }).nullable(),
  headline: z.string(),
  source_headline: z.string(),
  source_name: z.string(),
  source_hostname: z.string(),
  source_url: z.url(),
  source_published_at: z.iso.datetime({ offset: true }).nullable(),
  topic: newsTopicSchema,
  market: newsMarketSchema.nullable(),
  importance: z.number().int().min(1).max(5),
  event_key: z.string().nullable(),
  // Null for items published before candidates were recorded.
  candidate_id: z.uuid().nullable(),
})
export type NewsAdminItem = z.infer<typeof newsAdminItemSchema>
export const newsAdminCandidateSchema = z.object({
  id: z.uuid(),
  stage: newsCandidateStageSchema,
  drop_reason: newsCandidateDropReasonSchema.nullable(),
  headline: z.string(),
  source_name: z.string(),
  hostname: z.string(),
  url: z.url(),
  seen_at: z.iso.datetime({ offset: true }).nullable(),
  source_published_at: z.iso.datetime({ offset: true }).nullable(),
  // Filled only for candidates the model returned in some selection round.
  ai_rank: z.number().int().positive().nullable(),
  ai_topic: z.string().nullable(),
  ai_market: z.string().nullable(),
  ai_importance: z.number().int().nullable(),
  ai_event_key: z.string().nullable(),
  item_id: z.uuid().nullable(),
  publish_run_id: z.uuid().nullable(),
  publish_requested_at: z.iso.datetime({ offset: true }).nullable(),
  publish_error: z.string().nullable(),
})
export type NewsAdminCandidate = z.infer<typeof newsAdminCandidateSchema>
export const newsAdminEditionCountsSchema = z.object({
  discovered: z.number().int().nonnegative(),
  fetch_failed: z.number().int().nonnegative(),
  unused: z.number().int().nonnegative(),
  reviewed: z.number().int().nonnegative(),
  dropped: z.number().int().nonnegative(),
  published: z.number().int().nonnegative(),
  hidden: z.number().int().nonnegative(),
})
export type NewsAdminEditionCounts = z.infer<
  typeof newsAdminEditionCountsSchema
>
export const newsAdminEditionSchema = z.object({
  market_code: dataManagementNewsMarketCodeSchema,
  // Null when no edition exists for the date; the lists are then empty.
  edition: z
    .object({
      id: z.uuid(),
      revision: z.number().int().positive(),
      status: reportStatusSchema,
      generated_at: z.iso.datetime({ offset: true }),
      prompt_version: z.string(),
      target_items: z.number().int().positive(),
      counts: newsAdminEditionCountsSchema,
    })
    .nullable(),
  items: z.array(newsAdminItemSchema),
  candidates: z.array(newsAdminCandidateSchema),
})
export type NewsAdminEdition = z.infer<typeof newsAdminEditionSchema>
export const newsAdminEditionsSchema = z.object({
  edition_date: z.iso.date(),
  editions: z.array(newsAdminEditionSchema),
})
export type NewsAdminEditions = z.infer<typeof newsAdminEditionsSchema>
export const newsCandidatePublishInputSchema = z.object({
  edition_id: z.uuid(),
  candidate_ids: z.array(z.uuid()).min(1).max(10),
})
export type NewsCandidatePublishInput = z.infer<
  typeof newsCandidatePublishInputSchema
>

export const systemRoleSchema = z.enum(["admin", "asset_manager", "org_member"])
export type SystemRole = z.infer<typeof systemRoleSchema>

export const userSchema = z.object({
  id: z.uuid(),
  email: z.string(),
  display_name: z.string(),
  system_role: systemRoleSchema,
  status: z.literal("active"),
  must_change_password: z.boolean(),
  organization_id: z.uuid().nullable(),
})
export type User = z.infer<typeof userSchema>

export const authenticationSchema = z.object({
  user: userSchema,
  csrf_token: z.string().min(1),
})

export const csrfTokenSchema = z.object({
  csrf_token: z.string().min(1),
})

export const apiErrorSchema = z.object({
  detail: z.unknown().optional(),
})

export const organizationSchema = z.object({
  id: z.uuid(),
  name: z.string().min(1),
  slug: z.string().min(1),
  seat_limit: z.number().int().positive(),
  seat_count: z.number().int().nonnegative(),
  status: z.enum(["active", "archived"]),
  created_at: z.iso.datetime({ offset: true }),
  updated_at: z.iso.datetime({ offset: true }),
})
export type Organization = z.infer<typeof organizationSchema>
export const organizationListSchema = z.array(organizationSchema)

export const memberSchema = z.object({
  membership_id: z.uuid(),
  user_id: z.uuid(),
  email: z.string().min(1),
  display_name: z.string().min(1),
  status: z.enum(["active", "suspended", "invited"]),
  must_change_password: z.boolean(),
  joined_at: z.iso.datetime({ offset: true }),
})
export type Member = z.infer<typeof memberSchema>
export const memberListSchema = z.array(memberSchema)

export const provisionedMemberSchema = memberSchema.extend({
  temporary_password: z.string().min(1),
})
export type ProvisionedMember = z.infer<typeof provisionedMemberSchema>

export type OrganizationCreateInput = {
  name: string
  slug: string
  seat_limit: number
  contract_reference: string
  reason: string
}

export type MemberCreateInput = {
  email: string
  display_name: string
  reason: string
}

export type MemberUpdateInput = {
  display_name?: string
  status?: "active" | "suspended"
  reason: string
}

// A chapter marks where a segment of the audio starts; the segment runs to
// the next chapter's start (or the episode's end).
export const podcastChapterSchema = z.object({
  start_seconds: z.number().int().nonnegative(),
  title: z.string().min(1),
})
export type PodcastChapter = z.infer<typeof podcastChapterSchema>

export const podcastEpisodeSummarySchema = z.object({
  id: z.uuid(),
  trading_date: z.iso.date(),
  title: z.string().min(1),
  summary: z.string().min(1),
  locale: localeSchema,
  cover_asset_id: z.uuid().nullable(),
  duration_seconds: z.number().int().positive().nullable().default(null),
  // When the resolved audio file was registered; shown as the release time.
  audio_created_at: z.iso.datetime({ offset: true }).nullable().default(null),
  // Optional until the API publishes chapter markers; the player hides its
  // chapter list for an episode without any.
  chapters: z.array(podcastChapterSchema).default([]),
})
export type PodcastEpisodeSummary = z.infer<typeof podcastEpisodeSummarySchema>

export const podcastEpisodeListSchema = z.array(podcastEpisodeSummarySchema)

export const podcastEpisodeDetailSchema = podcastEpisodeSummarySchema.extend({
  published_at: z.iso.datetime({ offset: true }),
})
export type PodcastEpisodeDetail = z.infer<typeof podcastEpisodeDetailSchema>

export const podcastAudioPlaybackSchema = z.object({
  episode_id: z.uuid(),
  requested_locale: localeSchema,
  resolved_locale: localeSchema,
  asset_id: z.uuid(),
  url: z.url(),
  expires_in_seconds: z.number().int().positive(),
})
export type PodcastAudioPlayback = z.infer<typeof podcastAudioPlaybackSchema>

export const podcastMetadataSchema = z.object({
  locale: localeSchema,
  title: z.string().min(1).max(300),
  summary: z.string().min(1).max(10_000),
})
export type PodcastMetadata = z.infer<typeof podcastMetadataSchema>

export const podcastChaptersSourceSchema = z.enum(["none", "file", "manual"])
export const podcastMetadataSourceSchema = z.enum(["derived", "manual"])

export const podcastAudioVariantResponseSchema = z.object({
  asset_id: z.uuid(),
  locale: localeSchema,
  version: z.number().int().positive(),
  is_active: z.boolean(),
  duration_seconds: z.number().int().positive().nullable().default(null),
  chapters: z.array(podcastChapterSchema).default([]),
  chapters_source: podcastChaptersSourceSchema.default("none"),
})
export type PodcastAudioVariantResponse = z.infer<
  typeof podcastAudioVariantResponseSchema
>

export const podcastEpisodeAdminSchema = z.object({
  id: z.uuid(),
  trading_date: z.iso.date(),
  status: z.enum(["draft", "published"]),
  version: z.number().int().positive(),
  metadata: z.array(podcastMetadataSchema),
  metadata_source: podcastMetadataSourceSchema.default("derived"),
  audio_variants: z.array(podcastAudioVariantResponseSchema),
  cover_asset_id: z.uuid().nullable(),
  published_at: z.iso.datetime({ offset: true }).nullable(),
})
export type PodcastEpisodeAdmin = z.infer<typeof podcastEpisodeAdminSchema>

export const podcastEpisodeAdminListSchema = z.array(podcastEpisodeAdminSchema)

export type PodcastMetadataInput = {
  values: PodcastMetadata[]
}

export type PodcastEpisodeCreateInput = {
  trading_date: string
  metadata: PodcastMetadataInput
  reason: string
}

export type PodcastEpisodeUpdateInput = {
  expected_version: number
  metadata: PodcastMetadataInput
  reason: string
}

export type PodcastPublicationInput = {
  expected_version: number
}

export type PodcastChaptersUpdateInput = {
  expected_version: number
  chapters: PodcastChapter[]
  reason: string
}

export type PodcastAudioImportInput = {
  source_bucket: string
  source_key: string
  locale: Locale
  expected_mime_type: string
  confirm_replacement: boolean
  expected_current_version: number | null
  reason: string
}

export type PodcastUploadInput = {
  tradingDate: string
  reason: PodcastUploadReason
  files: Partial<Record<Locale, File>>
  confirmReplacement: boolean
  expectedVersions: Partial<Record<Locale, number>>
}

export type PodcastUploadReason = "initial_upload" | "update_file" | "other"
