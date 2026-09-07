import { afterEach, describe, expect, it, vi } from "vitest"
import openapi from "../openapi.json"
import {
  createAdministrationClient,
  createAuthClient,
  createBrowserTransport,
  createPodcastAdminClient,
  createPodcastClient,
  createMarketClient,
  createNewsClient,
  createReportClient,
} from "../src"
import { createServerTransport } from "../src/server"
import {
  indexMovingAveragesSchema,
  yfinanceDailyBarsResponseSchema,
} from "../src/schemas"

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

  it("requests and validates the authenticated latest-news contract", async () => {
    const transport = vi.fn(async () =>
      Response.json({
        market_code: "global",
        target_items: 5,
        edition_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        edition_date: "2026-09-01",
        revision: 1,
        generated_at: "2026-09-01T00:00:00+00:00",
        status: "partial",
        locale: "en",
        caveat: "1/5 stories completed",
        items: [],
      })
    )
    await expect(
      createNewsClient(transport).latest("en")
    ).resolves.toMatchObject({
      status: "partial",
    })
    expect(transport).toHaveBeenCalledWith("/api/news/latest?locale=en")
  })

  it("lists the organization's markets with their visibility", async () => {
    const transport = vi.fn(async () =>
      Response.json([
        {
          code: "crypto",
          is_visible: true,
          name_en: "Cryptocurrency",
          name_zh_hant: "加密貨幣",
          name_zh_hans: "加密货币",
        },
        {
          code: "forex",
          is_visible: false,
          name_en: "Foreign Exchange",
          name_zh_hant: "外匯",
          name_zh_hans: "外汇",
        },
      ])
    )
    await expect(createMarketClient(transport).list()).resolves.toEqual([
      expect.objectContaining({ code: "crypto", is_visible: true }),
      expect.objectContaining({ code: "forex", is_visible: false }),
    ])
    expect(transport).toHaveBeenCalledWith("/api/markets")
  })

  it("fetches index bars with an encoded symbol and optional date range", async () => {
    const bar = {
      symbol: "^TWII",
      market_code: "tw_equity",
      trade_date: "2026-09-02",
      open: "100.0000000000",
      high: "101.0000000000",
      low: "99.0000000000",
      close: "100.5000000000",
      volume: 1000,
    }
    const transport = vi.fn(async (path: string) =>
      Response.json(
        path.endsWith("/indices") ? [{ ...bar, previous_close: null }] : [bar]
      )
    )
    const client = createMarketClient(transport)
    await expect(client.latestIndexBars()).resolves.toEqual([
      expect.objectContaining({ symbol: "^TWII", previous_close: null }),
    ])
    expect(transport).toHaveBeenCalledWith("/api/markets/indices")
    await expect(
      client.indexDailyBars("^TWII", { start: "2026-01-01" })
    ).resolves.toHaveLength(1)
    expect(transport).toHaveBeenCalledWith(
      "/api/markets/indices/%5ETWII/daily-bars?start=2026-01-01"
    )
    await client.indexDailyBars("^TWII")
    expect(transport).toHaveBeenCalledWith(
      "/api/markets/indices/%5ETWII/daily-bars"
    )
  })

  it("fetches and strictly validates index moving averages", async () => {
    const response = {
      symbol: "^TWII",
      market_code: "tw_equity",
      method: "sma",
      price_field: "close",
      formula_version: "sma-close-v1",
      as_of: "2026-09-02",
      series: [20, 60, 120, 240].map(period => ({
        period,
        points: [{ trade_date: "2026-09-02", value: null }],
      })),
    }
    const transport = vi.fn(async () => Response.json(response))
    await expect(
      createMarketClient(transport).indexMovingAverages("^TWII", {
        start: "2026-01-01",
        end: "2026-09-02",
      })
    ).resolves.toEqual(response)
    expect(transport).toHaveBeenCalledWith(
      "/api/markets/indices/%5ETWII/moving-averages?start=2026-01-01&end=2026-09-02"
    )
    expect(
      indexMovingAveragesSchema.safeParse({ ...response, method: "ema" })
        .success
    ).toBe(false)
    expect(
      indexMovingAveragesSchema.safeParse({
        ...response,
        series: response.series.slice().reverse(),
      }).success
    ).toBe(false)
  })

  it("keeps generated moving-average series ordered and fixed-length", () => {
    const responseSchema =
      openapi.components.schemas.IndexMovingAveragesResponse
    expect(responseSchema).toMatchObject({
      properties: {
        series: {
          type: "array",
          minItems: 4,
          maxItems: 4,
          prefixItems: [
            { $ref: "#/components/schemas/IndexMovingAverage20SeriesResponse" },
            { $ref: "#/components/schemas/IndexMovingAverage60SeriesResponse" },
            {
              $ref: "#/components/schemas/IndexMovingAverage120SeriesResponse",
            },
            {
              $ref: "#/components/schemas/IndexMovingAverage240SeriesResponse",
            },
          ],
        },
      },
    })
  })

  it("fetches and validates TWSE institutional flow contracts", async () => {
    const contract = {
      as_of: "2026-09-04",
      contract_version: "twse-institutional-v1",
      contract_hash: "a".repeat(64),
      endpoint: "/rwd/zh/fund/BFI82U",
    }
    const transport = vi.fn(async (path: string) =>
      path.includes("institutional-flows")
        ? Response.json({
            ...contract,
            series: [
              {
                trade_date: "2026-09-04",
                foreign: "12.5",
                trust: "-1",
                dealer: "0.5",
                total: "12",
              },
            ],
          })
        : Response.json({
            ...contract,
            endpoint: "/rwd/en/fund/T86",
            rows: [
              {
                symbol: "2330",
                name: "TSMC",
                foreign_lots: "12.345",
                trust_lots: "2",
                dealer_lots: "-0.5",
                total_lots: "13.845",
              },
            ],
          })
    )
    const client = createMarketClient(transport)
    await expect(
      client.institutionalFlows({ start: "2026-07-01", end: "2026-09-04" })
    ).resolves.toMatchObject({ series: [{ total: "12" }] })
    await expect(
      client.institutionalStocks({ date: "2026-09-04", locale: "en" })
    ).resolves.toMatchObject({ rows: [{ total_lots: "13.845" }] })
    expect(transport).toHaveBeenCalledWith(
      "/api/markets/tw/institutional-stocks?date=2026-09-04&locale=en"
    )
  })

  it("refreshes the seven-day index window with CSRF protection", async () => {
    const transport = vi.fn(async () =>
      Response.json({
        period: "7d",
        fetched_at: "2026-09-04T00:00:00Z",
        succeeded: [
          {
            symbol: "^TWII",
            market: "tw_equity",
            as_of: "2026-09-03",
            stored_count: 5,
            dropped_unsettled_trade_date: "2026-09-04",
          },
        ],
        failed: [
          {
            symbol: "^HSI",
            market: "hk_equity",
            error: "provider unavailable",
          },
        ],
      })
    )

    const result =
      await createAdministrationClient(transport).refreshIndexDailyBars(
        "csrf-token"
      )

    expect(result.failed).toHaveLength(1)
    expect(transport).toHaveBeenCalledWith(
      "/api/admin/data-sources/yfinance/daily-bars",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "csrf-token",
        },
        body: JSON.stringify({ period: "7d" }),
      }
    )
  })

  it("rejects a refresh response outside the fixed seven-day contract", async () => {
    const client = createAdministrationClient(async () =>
      Response.json({
        period: "2y",
        fetched_at: "2026-09-04T00:00:00Z",
        succeeded: [],
        failed: [],
      })
    )

    await expect(
      client.refreshIndexDailyBars("csrf-token")
    ).rejects.toMatchObject({
      status: 502,
    })
  })

  it("publishes and validates the fixed seven-day refresh response period", () => {
    const responseSchema = openapi.components.schemas.YfinanceDailyBarsResponse

    expect(responseSchema.properties.period.const).toBe("7d")
    expect(
      yfinanceDailyBarsResponseSchema.safeParse({
        period: "7d",
        fetched_at: "2026-09-04T00:00:00Z",
        succeeded: [],
        failed: [],
      }).success
    ).toBe(true)
    expect(
      yfinanceDailyBarsResponseSchema.safeParse({
        period: "2y",
        fetched_at: "2026-09-04T00:00:00Z",
        succeeded: [],
        failed: [],
      }).success
    ).toBe(false)
  })

  it("accepts a stale analyst viewpoint status without replacing stored data", async () => {
    const client = createAdministrationClient(async () =>
      Response.json({
        viewpoint_date: "2026-09-02",
        fetched_at: "2026-09-02T01:00:00Z",
        status: "partial",
        markets: [
          {
            source_market_code: "us_macro",
            market_code: "global_macro_bonds",
            status: "stale",
          },
        ],
      })
    )

    await expect(
      client.syncAnalystViewpoints("csrf-token")
    ).resolves.toMatchObject({
      markets: [{ status: "stale" }],
    })
  })

  it("accepts analyst viewpoint status for every supported market", async () => {
    const viewpoints = [
      ["global_macro_bonds", "us_macro"],
      ["forex", "forex"],
      ["crypto", "crypto"],
      ["us_equity", "us_stocks"],
      ["hk_equity", "hk_stocks"],
      ["cn_equity", "cn_stocks"],
      ["tw_equity", "tw_stocks"],
      ["tw_index_derivatives", "tw_futures"],
    ].map(([market_code, source_market_code]) => ({
      viewpoint_date: "2026-09-03",
      market_code,
      source_market_code,
      points: ["A valid analyst viewpoint."],
      fetched_at: "2026-09-03T02:38:47.299301+00:00",
    }))
    const client = createAdministrationClient(async () =>
      Response.json({
        enabled: true,
        today: "2026-09-03",
        viewpoints,
        latest_sync: null,
      })
    )

    const result = await client.analystViewpointStatus()

    expect(result.viewpoints).toHaveLength(8)
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

  it("accepts only the three launch markets in report summaries", async () => {
    const summary = {
      publication_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
      report_key: "daily-market",
      market_code: "tw_equity",
      edition_date: "2026-08-30",
      revision: 1,
      source_as_of: null,
      published_at: "2026-08-30T00:00:00Z",
      stale: true,
      stale_reason: "source_too_old",
      status: "unavailable",
      title: "Taiwan",
      summary: null,
      locale: "en",
    }
    const client = createReportClient(async () => Response.json([summary]))
    await expect(client.list("en")).rejects.toMatchObject({ status: 502 })
  })

  it("creates an organization member with CSRF protection", async () => {
    let capturedPath = ""
    let captured: RequestInit | undefined
    const client = createAdministrationClient(async (path, init) => {
      capturedPath = path
      captured = init
      return Response.json(
        {
          membership_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
          user_id: "9322a09a-6a02-421b-a966-a5cd5f44056e",
          email: "member@example.com",
          display_name: "Member",
          status: "active",
          must_change_password: true,
          joined_at: "2026-07-27T03:00:00Z",
          temporary_password: "TemporaryPassword123!",
        },
        { status: 201 }
      )
    })

    const result = await client.createMember(
      "645f35b4-7e53-4ed3-a0c4-6bedc7db4e29",
      {
        email: "member@example.com",
        display_name: "Member",
        reason: "Provision seat",
      },
      "csrf-token"
    )

    expect(capturedPath).toBe(
      "/api/admin/organizations/645f35b4-7e53-4ed3-a0c4-6bedc7db4e29/members"
    )
    expect(captured?.method).toBe("POST")
    expect(new Headers(captured?.headers).get("x-csrf-token")).toBe(
      "csrf-token"
    )
    expect(JSON.parse(String(captured?.body))).toEqual({
      email: "member@example.com",
      display_name: "Member",
      reason: "Provision seat",
    })
    expect(result.temporary_password).toBe("TemporaryPassword123!")
  })

  it("rejects malformed organization responses", async () => {
    const client = createAdministrationClient(async () =>
      Response.json([
        {
          id: "not-a-uuid",
          name: "Organization",
          seat_limit: 3,
          seat_count: 0,
        },
      ])
    )

    await expect(client.listOrganizations()).rejects.toMatchObject({
      status: 502,
    })
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
