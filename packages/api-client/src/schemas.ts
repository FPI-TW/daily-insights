import { z } from "zod"

export const localeSchema = z.enum(["zh-hant", "zh-hans", "en"])
export type Locale = z.infer<typeof localeSchema>

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

export const podcastEpisodeSummarySchema = z.object({
  id: z.uuid(),
  trading_date: z.iso.date(),
  title: z.string().min(1),
  summary: z.string().min(1),
  locale: localeSchema,
  cover_asset_id: z.uuid().nullable(),
})
export type PodcastEpisodeSummary = z.infer<
  typeof podcastEpisodeSummarySchema
>

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
export type PodcastAudioPlayback = z.infer<
  typeof podcastAudioPlaybackSchema
>

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
