import { z } from "zod"

export const localeSchema = z.enum(["zh-hant", "zh-hans", "en"])
export type Locale = z.infer<typeof localeSchema>

export const launchMarketCodeSchema = z.enum([
  "global_macro_bonds",
  "crypto",
  "us_equity",
])
export type LaunchMarketCode = z.infer<typeof launchMarketCodeSchema>
export const reportStatusSchema = z.enum(["complete", "partial", "unavailable"])
export const blockStatusSchema = z.enum(["ok", "missing", "error"])
const decimalSchema = z.string().regex(/^-?\d+(?:\.\d+)?$/)
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

export const newsItemSchema = z.object({
  id: z.uuid(),
  rank: z.number().int().positive(),
  importance: z.number().int().min(1).max(5),
  topic: z.enum([
    "markets",
    "economy",
    "companies",
    "policy",
    "technology",
    "commodities",
  ]),
  headline: z.string(),
  summary: z.string(),
  source_name: z.string(),
  source_url: z.url(),
  source_published_at: z.iso.datetime({ offset: true }).nullable(),
})
export type NewsItem = z.infer<typeof newsItemSchema>
export const latestNewsSchema = z.object({
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

export const podcastEpisodeSummarySchema = z.object({
  id: z.uuid(),
  trading_date: z.iso.date(),
  title: z.string().min(1),
  summary: z.string().min(1),
  locale: localeSchema,
  cover_asset_id: z.uuid().nullable(),
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

export const podcastAudioVariantResponseSchema = z.object({
  asset_id: z.uuid(),
  locale: localeSchema,
  version: z.number().int().positive(),
  is_active: z.boolean(),
})

export const podcastEpisodeAdminSchema = z.object({
  id: z.uuid(),
  trading_date: z.iso.date(),
  status: z.enum(["draft", "published"]),
  version: z.number().int().positive(),
  metadata: z.array(podcastMetadataSchema),
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
