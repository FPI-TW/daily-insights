import { z } from "zod"
import {
  apiErrorSchema,
  authenticationSchema,
  csrfTokenSchema,
  podcastAudioPlaybackSchema,
  type PodcastAudioImportInput,
  type PodcastUploadInput,
  podcastEpisodeAdminListSchema,
  podcastEpisodeAdminSchema,
  type PodcastEpisodeCreateInput,
  podcastEpisodeDetailSchema,
  podcastEpisodeListSchema,
  type PodcastEpisodeUpdateInput,
  type Locale,
  type NewsMarketCode,
  type MemberCreateInput,
  memberListSchema,
  memberSchema,
  type MemberUpdateInput,
  type OrganizationCreateInput,
  organizationListSchema,
  organizationSchema,
  type PodcastPublicationInput,
  provisionedMemberSchema,
  reportDetailSchema,
  reportListSchema,
  latestNewsSchema,
  type LaunchMarketCode,
  userSchema,
} from "./schemas"

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly requestId: string | null,
    message: string,
    readonly detail: unknown = message
  ) {
    super(message)
    this.name = "ApiError"
  }
}

export function createReportClient(transport: ApiTransport) {
  return {
    async list(locale: Locale) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(`/api/reports?${query}`),
        reportListSchema
      )
    },
    async latest(marketCode: LaunchMarketCode, locale: Locale) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(`/api/reports/${marketCode}/latest?${query}`),
        reportDetailSchema
      )
    },
  }
}

export function createNewsClient(transport: ApiTransport) {
  return {
    async latest(locale: Locale) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(`/api/news/latest?${query}`),
        latestNewsSchema
      )
    },
    async latestForMarket(locale: Locale, marketCode: NewsMarketCode) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(
          `/api/news/${encodeURIComponent(marketCode)}/latest?${query}`
        ),
        latestNewsSchema
      )
    },
  }
}

export type ApiTransport = (
  path: string,
  init?: RequestInit
) => Promise<Response>

export function createBrowserTransport(): ApiTransport {
  return (path, init) =>
    fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...init?.headers,
      },
    })
}

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T>
): Promise<T> {
  if (!response.ok) {
    const parsed = apiErrorSchema.safeParse(
      await response.json().catch(() => ({}))
    )
    const detail = parsed.success ? parsed.data.detail : undefined
    const message =
      typeof detail === "string"
        ? detail
        : `API request failed (${response.status})`
    throw new ApiError(
      response.status,
      response.headers.get("X-Request-ID"),
      message,
      detail
    )
  }

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

export function createAuthClient(transport: ApiTransport) {
  return {
    async login(input: { email: string; password: string }) {
      const response = await transport("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      })
      return parseResponse(response, authenticationSchema)
    },
    async me() {
      return parseResponse(await transport("/api/auth/me"), userSchema)
    },
    async rotateCsrfToken() {
      return parseResponse(
        await transport("/api/auth/csrf", { method: "POST" }),
        csrfTokenSchema
      )
    },
    async changePassword(
      input: { current_password: string; new_password: string },
      csrfToken: string
    ) {
      const response = await transport("/api/auth/change-password", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(input),
      })
      return parseResponse(response, authenticationSchema)
    },
    async logout(csrfToken: string) {
      const response = await transport("/api/auth/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      })
      if (!response.ok) {
        throw new ApiError(
          response.status,
          response.headers.get("X-Request-ID"),
          "Unable to sign out"
        )
      }
    },
  }
}

export function createPodcastClient(transport: ApiTransport) {
  return {
    async list(locale: Locale) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(`/api/podcasts?${query}`),
        podcastEpisodeListSchema
      )
    },
    async get(episodeId: string, locale: Locale) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(`/api/podcasts/${episodeId}?${query}`),
        podcastEpisodeDetailSchema
      )
    },
    async createAudioUrl(episodeId: string, locale: Locale) {
      const query = new URLSearchParams({ locale })
      return parseResponse(
        await transport(`/api/podcasts/${episodeId}/audio-url?${query}`, {
          method: "POST",
        }),
        podcastAudioPlaybackSchema
      )
    },
  }
}

function mutationHeaders(csrfToken: string) {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
  }
}

export function createAdministrationClient(transport: ApiTransport) {
  return {
    async listOrganizations() {
      return parseResponse(
        await transport("/api/admin/organizations"),
        organizationListSchema
      )
    },
    async createOrganization(
      input: OrganizationCreateInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport("/api/admin/organizations", {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        organizationSchema
      )
    },
    async listMembers(organizationId: string) {
      return parseResponse(
        await transport(`/api/admin/organizations/${organizationId}/members`),
        memberListSchema
      )
    },
    async createMember(
      organizationId: string,
      input: MemberCreateInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport(`/api/admin/organizations/${organizationId}/members`, {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        provisionedMemberSchema
      )
    },
    async updateMember(
      organizationId: string,
      userId: string,
      input: MemberUpdateInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport(
          `/api/admin/organizations/${organizationId}/members/${userId}`,
          {
            method: "PATCH",
            headers: mutationHeaders(csrfToken),
            body: JSON.stringify(input),
          }
        ),
        memberSchema
      )
    },
    async removeMember(
      organizationId: string,
      userId: string,
      reason: string,
      csrfToken: string
    ) {
      const query = new URLSearchParams({ reason })
      const response = await transport(
        `/api/admin/organizations/${organizationId}/members/${userId}?${query}`,
        {
          method: "DELETE",
          headers: { "X-CSRF-Token": csrfToken },
        }
      )
      if (!response.ok) {
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
    },
  }
}

export function createPodcastAdminClient(transport: ApiTransport) {
  return {
    async list() {
      return parseResponse(
        await transport("/api/admin/podcasts"),
        podcastEpisodeAdminListSchema
      )
    },
    async create(input: PodcastEpisodeCreateInput, csrfToken: string) {
      return parseResponse(
        await transport("/api/admin/podcasts", {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        podcastEpisodeAdminSchema
      )
    },
    async update(
      episodeId: string,
      input: PodcastEpisodeUpdateInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport(`/api/admin/podcasts/${episodeId}`, {
          method: "PUT",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        podcastEpisodeAdminSchema
      )
    },
    async publish(
      episodeId: string,
      input: PodcastPublicationInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport(`/api/admin/podcasts/${episodeId}/publish`, {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        podcastEpisodeAdminSchema
      )
    },
    async unpublish(
      episodeId: string,
      input: PodcastPublicationInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport(`/api/admin/podcasts/${episodeId}/unpublish`, {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        podcastEpisodeAdminSchema
      )
    },
    async importAudio(
      episodeId: string,
      input: PodcastAudioImportInput,
      csrfToken: string
    ) {
      return parseResponse(
        await transport(`/api/admin/podcasts/${episodeId}/audio-imports`, {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(input),
        }),
        podcastEpisodeAdminSchema
      )
    },
    async upload(input: PodcastUploadInput, csrfToken: string) {
      const body = new FormData()
      body.set("trading_date", input.tradingDate)
      body.set("reason", input.reason)
      body.set("confirm_replacement", String(input.confirmReplacement))
      body.set("expected_versions", JSON.stringify(input.expectedVersions))
      const fieldNames: Record<Locale, string> = {
        "zh-hant": "zh_hant",
        "zh-hans": "zh_hans",
        en: "en",
      }
      for (const [locale, file] of Object.entries(input.files)) {
        if (file) body.set(fieldNames[locale as Locale], file)
      }
      return parseResponse(
        await transport("/api/admin/podcasts/uploads", {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken },
          body,
        }),
        podcastEpisodeAdminSchema
      )
    },
  }
}
