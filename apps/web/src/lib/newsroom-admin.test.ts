import { ApiError } from "@daily-insights/api-client"
import { describe, expect, it, vi } from "vitest"
import { event, item, source } from "#/components/newsroom-admin/test-fixtures"
import {
  createNewsroomAdminClient,
  formatTaipeiTime,
  itemAlerts,
  minutesUntil,
  moveItem,
  newsroomAdminErrorKey,
  reviewSearchSchema,
  sourceChanges,
  sourceFormDefaults,
  taipeiToday,
} from "./newsroom-admin"

describe("newsroom admin helpers", () => {
  it("dates editions in Taipei", () => {
    // 2026-09-30 23:30 UTC is already 1 October in Taipei.
    expect(taipeiToday(new Date("2026-09-30T23:30:00Z"))).toBe("2026-10-01")
    expect(formatTaipeiTime("2026-10-01T01:00:00+00:00")).toBe("09:00")
  })

  it("counts down to a deadline in whole minutes", () => {
    const deadline = "2026-10-01T01:00:00+00:00"
    const at = (iso: string) => new Date(iso).getTime()
    expect(minutesUntil(deadline, at("2026-10-01T00:30:30+00:00"))).toBe(30)
    expect(minutesUntil(deadline, at("2026-10-01T01:00:00+00:00"))).toBeNull()
  })

  it("moves an item one place and refuses to leave the list", () => {
    const items = [
      { id: "c", rank: 3 },
      { id: "a", rank: 1 },
      { id: "b", rank: 2 },
    ]
    expect(moveItem(items, "b", -1)).toEqual(["b", "a", "c"])
    expect(moveItem(items, "b", 1)).toEqual(["a", "c", "b"])
    expect(moveItem(items, "a", -1)).toBeNull()
    expect(moveItem(items, "missing", 1)).toBeNull()
  })

  it("lists the alerts a review card shows", () => {
    expect(itemAlerts(item())).toEqual([])
    expect(
      itemAlerts(
        item({
          why_status: "failed",
          abandoned_at: "2026-10-01T04:00:00+00:00",
          event: event({ analysis_status: "needs_body", body_ok_count: 0 }),
        })
      )
    ).toEqual(["abandoned", "whyFailed", "missingBody"])
    expect(
      itemAlerts(item({ event: event({ analysis_status: "pending" }) }))
    ).toEqual(["analysisPending"])
  })

  it("ignores malformed review search params", () => {
    expect(reviewSearchSchema.parse({ date: "nope", market: "asia" })).toEqual(
      {}
    )
    expect(
      reviewSearchSchema.parse({ date: "2026-10-01", market: "tw_equity" })
    ).toEqual({ date: "2026-10-01", market: "tw_equity" })
  })

  it("sends only changed source fields and never the manual kind", () => {
    const current = source()
    expect(sourceChanges(current, sourceFormDefaults(current))).toEqual({})
    expect(
      sourceChanges(current, {
        ...sourceFormDefaults(current),
        link_pattern: "/markets/",
        language_filter: "",
      })
    ).toEqual({ link_pattern: "/markets/", language_filter: null })
    const manual = source({ kind: "manual" })
    expect(
      sourceChanges(manual, { ...sourceFormDefaults(manual), name: "Manual" })
    ).toEqual({ name: "Manual" })
  })

  it("maps API failures to messages", () => {
    expect(newsroomAdminErrorKey(new ApiError(409, null, "x"))).toBe(
      "newsroomAdminErrorConflict"
    )
    expect(newsroomAdminErrorKey(new ApiError(422, null, "x"))).toBe(
      "newsroomAdminErrorInvalid"
    )
    expect(newsroomAdminErrorKey(new Error("x"))).toBe(
      "newsroomAdminErrorFailed"
    )
  })
})

describe("newsroom admin client", () => {
  it("sends writes with the CSRF token and JSON body", async () => {
    const transport = vi.fn(
      async (_path: string, _init?: RequestInit) =>
        new Response(null, { status: 204 })
    )
    const client = createNewsroomAdminClient(transport)

    await client.reorder("edition-1", ["b", "a"], "token")

    const [path, init] = transport.mock.calls[0]!
    expect(path).toBe("/api/admin/newsroom/editions/edition-1/order")
    expect(init?.method).toBe("PUT")
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("token")
    expect(JSON.parse(String(init?.body))).toEqual({ item_ids: ["b", "a"] })
  })

  it("surfaces the API detail and rejects invalid payloads", async () => {
    const conflict = createNewsroomAdminClient(async () =>
      Response.json({ detail: "edition is already published" }, { status: 409 })
    )
    await expect(conflict.publishEdition("e", "t")).rejects.toMatchObject({
      status: 409,
      message: "edition is already published",
    })
    const malformed = createNewsroomAdminClient(async () =>
      Response.json({ sources: [{ id: "not-a-source" }] })
    )
    await expect(malformed.listSources()).rejects.toMatchObject({
      status: 502,
    })
  })
})
