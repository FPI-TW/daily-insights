import {
  ApiError,
  apiErrorSchema,
  createBrowserTransport,
  type ApiTransport,
  type components,
} from "@daily-insights/api-client"
import { z } from "zod"

// Client, response schemas and query keys for the newsroom review console
// (/api/admin/newsroom). Every schema is checked against the generated
// OpenAPI types with `satisfies`, so a backend contract change fails the
// type check instead of drifting silently.

type Schemas = components["schemas"]

export const newsroomMarkets = ["global", "tw_equity", "us_equity"] as const
export type NewsroomMarket = (typeof newsroomMarkets)[number]
export const newsroomSourceKinds = [
  "rss",
  "rdf",
  "atom",
  "rss_full",
  "news_sitemap",
  "json_list",
  "guardian_api",
] as const

const timestamp = z.iso.datetime({ offset: true })
const marketSchema = z.enum(newsroomMarkets)

export const newsroomSourceSchema = z.object({
  id: z.uuid(),
  key: z.string(),
  name: z.string(),
  kind: z.enum([...newsroomSourceKinds, "manual"]),
  url: z.string().nullable(),
  hostname: z.string(),
  link_pattern: z.string().nullable(),
  markets: z.array(marketSchema),
  language_filter: z.array(z.string()).nullable(),
  full_text_in_feed: z.boolean(),
  trust_tier: z.number().int(),
  weight: z.number(),
  poll_interval_minutes: z.number().int(),
  enabled: z.boolean(),
  last_polled_at: timestamp.nullable(),
  last_success_at: timestamp.nullable(),
  next_poll_at: timestamp.nullable(),
  consecutive_failures: z.number().int(),
  last_error_code: z.string().nullable(),
  last_error_at: timestamp.nullable(),
  articles_7d: z.number().int(),
  health: z.enum(["disabled", "pending", "healthy", "degraded", "unhealthy"]),
}) satisfies z.ZodType<Schemas["NewsroomAdminSource"]>
export type NewsroomSource = z.infer<typeof newsroomSourceSchema>
export type NewsroomSourceHealth = NewsroomSource["health"]

const sourceListSchema = z.object({
  sources: z.array(newsroomSourceSchema),
}) satisfies z.ZodType<Schemas["NewsroomAdminSourceList"]>

const bodyStatusSchema = z.enum([
  "pending",
  "ok",
  "unavailable",
  "rejected",
  "purged",
])
const queueStatusSchema = z.enum(["idle", "pending", "done", "failed"])
const translationStatusSchema = z.enum(["idle", "pending", "ready", "failed"])

const eventSchema = z.object({
  id: z.uuid(),
  edition_date: z.iso.date(),
  working_title: z.string(),
  status: z.enum(["open", "merged"]),
  merged_into_id: z.uuid().nullable(),
  created_by: z.enum(["triage", "split", "manual", "legacy"]),
  headline_zh_hant: z.string().nullable(),
  summary_zh_hant: z.string().nullable(),
  headline_zh_hans: z.string().nullable(),
  summary_zh_hans: z.string().nullable(),
  headline_en: z.string().nullable(),
  summary_en: z.string().nullable(),
  related_symbols: z.array(
    z.object({
      symbol: z.string(),
      kind: z.enum(["index", "equity", "fx", "commodity", "rate", "crypto"]),
      label: z.string(),
    })
  ),
  analysis_status: z.enum(["idle", "pending", "ready", "failed", "needs_body"]),
  analysis_error_code: z.string().nullable(),
  analyzed_at: timestamp.nullable(),
  en_status: translationStatusSchema,
  en_error_code: z.string().nullable(),
  edited_at: timestamp.nullable(),
  article_count: z.number().int(),
  source_count: z.number().int(),
  body_ok_count: z.number().int(),
  articles: z.array(
    z.object({
      id: z.uuid(),
      source_name: z.string(),
      hostname: z.string(),
      title: z.string(),
      url: z.string(),
      published_at: timestamp.nullable(),
      body_status: bodyStatusSchema,
    })
  ),
}) satisfies z.ZodType<Schemas["NewsroomAdminEvent"]>
export type NewsroomEvent = z.infer<typeof eventSchema>
export type NewsroomRelatedSymbol = NewsroomEvent["related_symbols"][number]

const editionSchema = z.object({
  id: z.uuid(),
  edition_date: z.iso.date(),
  market_code: marketSchema,
  status: z.enum(["draft", "published"]),
  selection_mode: z.enum(["pending", "editor", "fallback", "legacy"]),
  auto_publish_at: timestamp,
  late_fill_deadline: timestamp,
  assembled_at: timestamp.nullable(),
  published_at: timestamp.nullable(),
  published_by_user_id: z.uuid().nullable(),
  ignored_pending_triage: z.number().int(),
  late_fill_closed_at: timestamp.nullable(),
}) satisfies z.ZodType<Schemas["NewsroomAdminEdition"]>
export type NewsroomEdition = z.infer<typeof editionSchema>

const itemSchema = z.object({
  id: z.uuid(),
  edition_id: z.uuid(),
  rank: z.number().int(),
  stars: z.number().int().nullable(),
  editor_score: z.number().nullable(),
  origin: z.enum(["model", "manual", "legacy"]),
  why_zh_hant: z.string().nullable(),
  why_zh_hans: z.string().nullable(),
  why_en: z.string().nullable(),
  why_status: z.enum(["pending", "ready", "failed"]),
  why_error_code: z.string().nullable(),
  why_en_status: translationStatusSchema,
  removed_at: timestamp.nullable(),
  hidden_at: timestamp.nullable(),
  abandoned_at: timestamp.nullable(),
  event: eventSchema,
}) satisfies z.ZodType<Schemas["NewsroomAdminItem"]>
export type NewsroomItem = z.infer<typeof itemSchema>

const editionDetailSchema = z.object({
  edition_date: z.iso.date(),
  market_code: marketSchema,
  edition: editionSchema.nullable(),
  items: z.array(itemSchema),
  candidates: z.array(z.object({ score: z.number(), event: eventSchema })),
}) satisfies z.ZodType<Schemas["NewsroomAdminEditionDetail"]>
export type NewsroomEditionDetail = z.infer<typeof editionDetailSchema>
export type NewsroomCandidate = NewsroomEditionDetail["candidates"][number]

const itemCountsSchema = z.object({
  active: z.number().int(),
  removed: z.number().int(),
  hidden: z.number().int(),
  abandoned: z.number().int(),
  ready: z.number().int(),
  analysis_failed: z.number().int(),
  needs_body: z.number().int(),
}) satisfies z.ZodType<Schemas["NewsroomAdminItemCounts"]>

const editionDaySchema = z.object({
  edition_date: z.iso.date(),
  is_today: z.boolean(),
  untriaged_articles: z.number().int(),
  triage_failed_articles: z.number().int(),
  markets: z.array(
    z.object({
      market_code: marketSchema,
      edition: editionSchema.nullable(),
      counts: itemCountsSchema,
    })
  ),
}) satisfies z.ZodType<Schemas["NewsroomAdminEditionDay"]>
export type NewsroomEditionDay = z.infer<typeof editionDaySchema>

const articleSchema = z.object({
  id: z.uuid(),
  source_id: z.uuid(),
  source_key: z.string(),
  source_name: z.string(),
  hostname: z.string(),
  title: z.string(),
  url: z.string(),
  feed_summary: z.string().nullable(),
  published_at: timestamp.nullable(),
  first_seen_at: timestamp,
  language: z.string().nullable(),
  body_status: bodyStatusSchema,
  body_source: z.enum(["feed", "fetch", "manual"]).nullable(),
  body_quality_reason: z.string().nullable(),
  body_fetched_at: timestamp.nullable(),
  body_length: z.number().int(),
  body_preview: z.string().nullable(),
  fetch_status: queueStatusSchema,
  fetch_error_code: z.string().nullable(),
  embed_status: queueStatusSchema,
  triage_status: queueStatusSchema,
  triage_error_code: z.string().nullable(),
  relevant: z.boolean().nullable(),
  topic: z.string().nullable(),
  market_scores: z.record(z.string(), z.number()),
}) satisfies z.ZodType<Schemas["NewsroomAdminArticle"]>
export type NewsroomArticle = z.infer<typeof articleSchema>

const eventDetailSchema = z.object({
  event: eventSchema,
  articles: z.array(articleSchema),
  placements: z.array(
    z.object({
      item_id: z.uuid(),
      edition_id: z.uuid(),
      market_code: marketSchema,
      edition_status: z.enum(["draft", "published"]),
      removed: z.boolean(),
      hidden: z.boolean(),
    })
  ),
}) satisfies z.ZodType<Schemas["NewsroomAdminEventDetail"]>
export type NewsroomEventDetail = z.infer<typeof eventDetailSchema>

const createdItemSchema = z.object({
  item_id: z.uuid(),
}) satisfies z.ZodType<Schemas["NewsroomAdminCreatedItem"]>
const createdEventSchema = z.object({
  event_id: z.uuid(),
}) satisfies z.ZodType<Schemas["NewsroomAdminCreatedEvent"]>
const createdArticleSchema = z.object({
  article_id: z.uuid(),
}) satisfies z.ZodType<Schemas["NewsroomAdminCreatedArticle"]>

export type NewsroomSourceCreate = Schemas["NewsroomAdminSourceCreate"]
export type NewsroomSourceUpdate = Schemas["NewsroomAdminSourceUpdate"]
export type NewsroomEventEdit = Schemas["NewsroomAdminEventEdit"]

async function readError(response: Response): Promise<never> {
  const parsed = apiErrorSchema.safeParse(
    await response.json().catch(() => ({}))
  )
  const detail = parsed.success ? parsed.data.detail : undefined
  throw new ApiError(
    response.status,
    response.headers.get("X-Request-ID"),
    typeof detail === "string"
      ? detail
      : `API request failed (${response.status})`,
    detail
  )
}

async function parse<T>(response: Response, schema: z.ZodType<T>) {
  if (!response.ok) return readError(response)
  const result = schema.safeParse(await response.json())
  if (!result.success) {
    throw new ApiError(
      502,
      response.headers.get("X-Request-ID"),
      "API response failed schema validation"
    )
  }
  return result.data
}

async function expectEmpty(response: Response) {
  if (!response.ok) await readError(response)
}

const BASE = "/api/admin/newsroom"

export function createNewsroomAdminClient(transport: ApiTransport) {
  const send = (
    path: string,
    method: string,
    csrfToken: string,
    body?: unknown
  ) =>
    transport(`${BASE}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  const id = encodeURIComponent

  return {
    async editionDay(date: string) {
      const query = new URLSearchParams({ date })
      return parse(
        await transport(`${BASE}/editions?${query}`),
        editionDaySchema
      )
    },
    async editionDetail(date: string, market: NewsroomMarket) {
      return parse(
        await transport(`${BASE}/editions/${id(date)}/${id(market)}`),
        editionDetailSchema
      )
    },
    async eventDetail(eventId: string) {
      return parse(
        await transport(`${BASE}/events/${id(eventId)}`),
        eventDetailSchema
      )
    },
    async listSources() {
      return parse(await transport(`${BASE}/sources`), sourceListSchema)
    },
    async createSource(input: NewsroomSourceCreate, csrfToken: string) {
      return parse(
        await send("/sources", "POST", csrfToken, input),
        newsroomSourceSchema
      )
    },
    async updateSource(
      sourceId: string,
      changes: NewsroomSourceUpdate,
      csrfToken: string
    ) {
      return parse(
        await send(`/sources/${id(sourceId)}`, "PATCH", csrfToken, changes),
        newsroomSourceSchema
      )
    },
    async publishEdition(editionId: string, csrfToken: string) {
      await expectEmpty(
        await send(`/editions/${id(editionId)}/publish`, "POST", csrfToken)
      )
    },
    async publishDay(date: string, csrfToken: string) {
      await expectEmpty(
        await send("/editions/publish", "POST", csrfToken, {
          edition_date: date,
        })
      )
    },
    async addItem(editionId: string, eventId: string, csrfToken: string) {
      return parse(
        await send(`/editions/${id(editionId)}/items`, "POST", csrfToken, {
          event_id: eventId,
        }),
        createdItemSchema
      )
    },
    async reorder(editionId: string, itemIds: string[], csrfToken: string) {
      await expectEmpty(
        await send(`/editions/${id(editionId)}/order`, "PUT", csrfToken, {
          item_ids: itemIds,
        })
      )
    },
    async itemAction(
      itemId: string,
      action: "remove" | "restore" | "hide" | "unhide",
      csrfToken: string
    ) {
      await expectEmpty(
        await send(`/items/${id(itemId)}/${action}`, "POST", csrfToken)
      )
    },
    async editWhy(itemId: string, why: string, csrfToken: string) {
      await expectEmpty(
        await send(`/items/${id(itemId)}/why`, "PUT", csrfToken, { why })
      )
    },
    async editEvent(
      eventId: string,
      changes: NewsroomEventEdit,
      csrfToken: string
    ) {
      await expectEmpty(
        await send(`/events/${id(eventId)}`, "PATCH", csrfToken, changes)
      )
    },
    async reanalyze(eventId: string, csrfToken: string) {
      await expectEmpty(
        await send(`/events/${id(eventId)}/reanalyze`, "POST", csrfToken)
      )
    },
    async mergeEvents(
      targetId: string,
      sourceIds: string[],
      csrfToken: string
    ) {
      await expectEmpty(
        await send("/events/merge", "POST", csrfToken, {
          target_id: targetId,
          source_ids: sourceIds,
        })
      )
    },
    async splitEvent(eventId: string, articleIds: string[], csrfToken: string) {
      return parse(
        await send(`/events/${id(eventId)}/split`, "POST", csrfToken, {
          article_ids: articleIds,
        }),
        createdEventSchema
      )
    },
    async setManualBody(articleId: string, body: string, csrfToken: string) {
      await expectEmpty(
        await send(`/articles/${id(articleId)}/body`, "PUT", csrfToken, {
          body,
        })
      )
    },
    async submitManualUrl(url: string, date: string, csrfToken: string) {
      return parse(
        await send("/articles/manual", "POST", csrfToken, {
          url,
          edition_date: date,
        }),
        createdArticleSchema
      )
    },
  }
}

export function browserNewsroomAdminClient() {
  return createNewsroomAdminClient(createBrowserTransport())
}

// Query keys carry every input of the request. An event edit is shared by
// every market of its date, so `editionsOfDate` invalidates all three
// market details at once without touching other dates.
export const newsroomAdminKeys = {
  all: ["newsroom-admin"] as const,
  day: (date: string) => ["newsroom-admin", "day", date] as const,
  editionsOfDate: (date: string) =>
    ["newsroom-admin", "edition", date] as const,
  edition: (date: string, market: NewsroomMarket) =>
    ["newsroom-admin", "edition", date, market] as const,
  event: (eventId: string) => ["newsroom-admin", "event", eventId] as const,
  sources: () => ["newsroom-admin", "sources"] as const,
}

export const reviewSearchSchema = z.object({
  date: z.iso.date().optional().catch(undefined),
  market: marketSchema.optional().catch(undefined),
})

/** Today's edition date: editions are dated in Asia/Taipei (spec §3). */
export function taipeiToday(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now)
}

/** Whole minutes until `deadline`, or null once it has passed. */
export function minutesUntil(deadline: string, now: number) {
  const remaining = new Date(deadline).getTime() - now
  if (!Number.isFinite(remaining) || remaining <= 0) return null
  return Math.ceil(remaining / 60_000)
}

/** Items in rank order with `itemId` swapped with its neighbour. */
export function moveItem(
  items: readonly Pick<NewsroomItem, "id" | "rank">[],
  itemId: string,
  direction: -1 | 1
) {
  const ordered = [...items].sort((a, b) => a.rank - b.rank)
  const index = ordered.findIndex(item => item.id === itemId)
  const target = index + direction
  if (index < 0 || target < 0 || target >= ordered.length) return null
  const ids = ordered.map(item => item.id)
  const [moved] = ids.splice(index, 1)
  ids.splice(target, 0, itemId)
  return moved === undefined ? null : ids
}

/** Review states an editor must notice on a card, most urgent first. */
export function itemAlerts(item: NewsroomItem) {
  const alerts: Array<
    | "abandoned"
    | "analysisFailed"
    | "whyFailed"
    | "missingBody"
    | "analysisPending"
    | "whyPending"
  > = []
  if (item.abandoned_at) alerts.push("abandoned")
  if (item.event.analysis_status === "failed") alerts.push("analysisFailed")
  if (item.why_status === "failed") alerts.push("whyFailed")
  if (
    item.event.analysis_status === "needs_body" ||
    item.event.body_ok_count === 0
  ) {
    alerts.push("missingBody")
  }
  if (
    item.event.analysis_status === "pending" ||
    item.event.analysis_status === "idle"
  ) {
    alerts.push("analysisPending")
  } else if (item.why_status === "pending") {
    alerts.push("whyPending")
  }
  return alerts
}

/** True while background stages can still change what the page shows. */
export function hasPendingWork(detail: NewsroomEditionDetail | undefined) {
  return (
    detail?.items.some(
      item =>
        item.why_status === "pending" ||
        item.event.analysis_status === "pending"
    ) ?? false
  )
}

/** i18n key for a failed newsroom admin mutation. */
export function newsroomAdminErrorKey(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 409) return "newsroomAdminErrorConflict"
    if (error.status === 422) return "newsroomAdminErrorInvalid"
    if (error.status === 404) return "newsroomAdminErrorNotFound"
  }
  return "newsroomAdminErrorFailed"
}

/** "HH:mm" in Asia/Taipei, the clock every newsroom deadline is set in. */
export function formatTaipeiTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return "—"
  return new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Taipei",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date)
}

// --- Source form --------------------------------------------------------------

const SOURCE_KEY = /^[a-z0-9][a-z0-9_-]*$/
const HOSTNAME =
  /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/
const HTTP_URL = /^https?:\/\/\S+$/i

function isRegex(value: string) {
  try {
    new RegExp(value)
    return true
  } catch {
    return false
  }
}

function languageCodes(value: string) {
  return value
    .split(",")
    .map(code => code.trim().toLowerCase())
    .filter(Boolean)
}

// Messages are i18n keys; the form translates them for display. Bounds
// mirror the API's request schema (newsroom/admin_api.py).
export const sourceFormSchema = z.object({
  key: z
    .string()
    .trim()
    .max(80, "newsroomAdminSourceErrorKey")
    .regex(SOURCE_KEY, "newsroomAdminSourceErrorKey"),
  name: z
    .string()
    .trim()
    .min(1, "newsroomAdminSourceErrorRequired")
    .max(200, "newsroomAdminSourceErrorRequired"),
  kind: z.enum(newsroomSourceKinds),
  url: z
    .string()
    .trim()
    .max(2_000, "newsroomAdminSourceErrorUrl")
    .refine(value => value === "" || HTTP_URL.test(value), {
      message: "newsroomAdminSourceErrorUrl",
    }),
  hostname: z
    .string()
    .trim()
    .refine(value => HOSTNAME.test(value.toLowerCase()), {
      message: "newsroomAdminSourceErrorHostname",
    }),
  markets: z.array(marketSchema),
  trust_tier: z
    .number()
    .int()
    .min(1, "newsroomAdminSourceErrorTrust")
    .max(3, "newsroomAdminSourceErrorTrust"),
  weight: z
    .number()
    .min(0.5, "newsroomAdminSourceErrorWeight")
    .max(2, "newsroomAdminSourceErrorWeight"),
  poll_interval_minutes: z
    .number()
    .int("newsroomAdminSourceErrorInterval")
    .min(5, "newsroomAdminSourceErrorInterval")
    .max(1440, "newsroomAdminSourceErrorInterval"),
  enabled: z.boolean(),
  link_pattern: z
    .string()
    .max(500, "newsroomAdminSourceErrorPattern")
    .refine(value => value === "" || isRegex(value), {
      message: "newsroomAdminSourceErrorPattern",
    }),
  language_filter: z
    .string()
    .refine(
      value =>
        languageCodes(value).every(
          code => code.length >= 2 && code.length <= 10
        ),
      { message: "newsroomAdminSourceErrorLanguages" }
    ),
  full_text_in_feed: z.boolean(),
})
export type SourceFormValues = z.input<typeof sourceFormSchema>

export function sourceFormDefaults(
  source?: NewsroomSource | null
): SourceFormValues {
  return {
    key: source?.key ?? "",
    name: source?.name ?? "",
    kind:
      source && source.kind !== "manual" ? source.kind : newsroomSourceKinds[0],
    url: source?.url ?? "",
    hostname: source?.hostname ?? "",
    markets: source?.markets ?? [],
    trust_tier: source?.trust_tier ?? 2,
    weight: source?.weight ?? 1,
    poll_interval_minutes: source?.poll_interval_minutes ?? 30,
    enabled: source?.enabled ?? true,
    link_pattern: source?.link_pattern ?? "",
    language_filter: source?.language_filter?.join(", ") ?? "",
    full_text_in_feed: source?.full_text_in_feed ?? false,
  }
}

function sourcePayload(values: SourceFormValues) {
  const languages = languageCodes(values.language_filter)
  return {
    key: values.key.trim(),
    name: values.name.trim(),
    kind: values.kind,
    url: values.url.trim() || null,
    hostname: values.hostname.trim().toLowerCase(),
    markets: values.markets,
    trust_tier: values.trust_tier,
    weight: values.weight,
    poll_interval_minutes: values.poll_interval_minutes,
    enabled: values.enabled,
    link_pattern: values.link_pattern || null,
    language_filter: languages.length > 0 ? languages : null,
    full_text_in_feed: values.full_text_in_feed,
  } satisfies NewsroomSourceCreate
}

export function sourceCreateInput(
  values: SourceFormValues
): NewsroomSourceCreate {
  return sourcePayload(values)
}

/** Only the fields the editor changed, so a PATCH never rewrites the rest. */
export function sourceChanges(
  source: NewsroomSource,
  values: SourceFormValues
): NewsroomSourceUpdate {
  const next = sourcePayload(values)
  const current = sourcePayload(sourceFormDefaults(source))
  const changes: Record<string, unknown> = {}
  for (const name of Object.keys(next) as Array<keyof typeof next>) {
    if (JSON.stringify(next[name]) !== JSON.stringify(current[name])) {
      changes[name] = next[name]
    }
  }
  // The fixed manual source keeps its kind; the form cannot show "manual".
  if (source.kind === "manual") delete changes.kind
  return changes as NewsroomSourceUpdate
}
