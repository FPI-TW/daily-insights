import { afterEach, describe, expect, it, vi } from "vitest"
import {
  createAuthClient,
  createBrowserTransport,
  createPodcastAdminClient,
  createPodcastClient,
} from "../src"
import { createServerTransport } from "../src/server"

describe("API client trust boundary", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("rejects a successful response with an invalid body", async () => {
    const client = createAuthClient(async () =>
      Response.json({ id: "not-a-user" })
    )

    await expect(client.me()).rejects.toMatchObject({
      status: 502,
      message: "API response failed schema validation",
    })
  })

  it("uses same-origin credentials in the browser", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(Response.json({}))

    await createBrowserTransport()("/api/health/live")

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/health/live",
      expect.objectContaining({ credentials: "same-origin" })
    )
  })

  it("forwards only approved server headers", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(Response.json({}))
    const transport = createServerTransport("http://api:8000", {
      cookie: "daily_insights_session=approved",
      requestId: "approved-request",
    })

    await transport("/api/auth/me", {
      headers: {
        Authorization: "Bearer rejected",
        Cookie: "attacker=smuggled",
        Host: "attacker.invalid",
        "X-Forwarded-For": "203.0.113.7",
        "Content-Type": "application/json",
      },
    })

    const init = fetchMock.mock.calls[0]?.[1]
    const forwarded = new Headers(init?.headers)
    expect(forwarded.get("cookie")).toBe("daily_insights_session=approved")
    expect(forwarded.get("x-request-id")).toBe("approved-request")
    expect(forwarded.get("content-type")).toBe("application/json")
    expect(forwarded.has("authorization")).toBe(false)
    expect(forwarded.has("host")).toBe(false)
    expect(forwarded.has("x-forwarded-for")).toBe(false)
  })

  it("validates localized Podcast catalog responses", async () => {
    const client = createPodcastClient(async () =>
      Response.json([
        {
          id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
          trading_date: "2026-07-24",
          title: "市場晨報",
          summary: "今日摘要",
          locale: "zh-hant",
          cover_asset_id: null,
        },
      ])
    )

    await expect(client.list("zh-hant")).resolves.toHaveLength(1)
  })

  it("rejects an invalid Podcast locale returned by the API", async () => {
    const client = createPodcastClient(async () =>
      Response.json([
        {
          id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
          trading_date: "2026-07-24",
          title: "Market Brief",
          summary: "Summary",
          locale: "fr",
          cover_asset_id: null,
        },
      ])
    )

    await expect(client.list("en")).rejects.toMatchObject({ status: 502 })
  })

  it("uploads one to three Podcast files as multipart without forcing content type", async () => {
    let captured: RequestInit | undefined
    const client = createPodcastAdminClient(async (_path, init) => {
      captured = init
      return Response.json({
        id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        trading_date: "2026-07-25",
        status: "draft",
        version: 2,
        metadata: [
          {
            locale: "zh-hant",
            title: "Podcast | 2026-07-25",
            summary: "2026-07-25",
          },
        ],
        audio_variants: [],
        cover_asset_id: null,
        published_at: null,
      })
    })

    await client.upload(
      {
        tradingDate: "2026-07-25",
        reason: "initial_upload",
        files: {
          en: new File(["podcast"], "source-name.mp3", {
            type: "audio/mpeg",
          }),
        },
        confirmReplacement: false,
        expectedVersions: {},
      },
      "csrf-token"
    )

    expect(captured?.body).toBeInstanceOf(FormData)
    const headers = new Headers(captured?.headers)
    expect(headers.get("x-csrf-token")).toBe("csrf-token")
    expect(headers.has("content-type")).toBe(false)
    expect((captured?.body as FormData).get("en")).toBeInstanceOf(File)
  })
})
