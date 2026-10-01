import { describe, expect, it } from "vitest"
import type { NewsFeedPollSource } from "@daily-insights/api-client"
import {
  feedPollHealth,
  sortFeedPollSources,
  summarizeFeedPolls,
} from "./news-collection"

const now = Date.parse("2026-09-30T00:00:00Z")
const polled = {
  last_attempt_at: "2026-09-29T23:00:00Z",
  last_success_at: "2026-09-29T23:00:00Z",
  cooldown_until: null,
  last_error_code: null,
}

describe("feedPollHealth", () => {
  it("reports a feed the collector has not polled yet", () => {
    expect(feedPollHealth({ ...polled, last_attempt_at: null }, now)).toBe(
      "never"
    )
  })

  it("reports a successful latest poll as ok", () => {
    expect(feedPollHealth(polled, now)).toBe("ok")
  })

  it("reports an active cooldown before the failure", () => {
    expect(
      feedPollHealth(
        {
          ...polled,
          last_error_code: "rate_limited",
          cooldown_until: "2026-09-30T00:15:00Z",
        },
        now
      )
    ).toBe("cooling")
  })

  it("treats an expired cooldown and a stale success as failing", () => {
    expect(
      feedPollHealth(
        {
          ...polled,
          last_success_at: "2026-09-29T21:00:00Z",
          cooldown_until: "2026-09-29T23:30:00Z",
        },
        now
      )
    ).toBe("failing")
    expect(feedPollHealth({ ...polled, last_success_at: null }, now)).toBe(
      "failing"
    )
    expect(
      feedPollHealth({ ...polled, last_error_code: "parse_error" }, now)
    ).toBe("failing")
  })
})

function source(
  name: string,
  overrides: Partial<NewsFeedPollSource> = {}
): NewsFeedPollSource {
  return {
    source_key: name.padEnd(64, "0"),
    source_name: name,
    hostname: "example.com",
    feed_url: "https://example.com/rss",
    registered: true,
    poll_group: "fast",
    markets: ["global"],
    last_count: 10,
    last_status: 200,
    consecutive_failures: 0,
    last_gap_minutes: null,
    gap_count: 0,
    gap_count_since: "2026-09-30",
    ...polled,
    ...overrides,
  }
}

describe("feed poll summary and ordering", () => {
  const sources = [
    source("Bravo"),
    source("Alpha"),
    source("Gap", { gap_count: 2 }),
    source("Never", { last_attempt_at: null, last_success_at: null }),
    source("Cool", {
      last_error_code: "rate_limited",
      cooldown_until: "2026-09-30T00:15:00Z",
    }),
    source("Fail", { last_success_at: null, last_error_code: "timeout" }),
    source("Also gap", { gap_count: 1, last_error_code: "parse_error" }),
  ]

  it("counts healthy, troubled and never-polled feeds and tonight's gaps", () => {
    expect(summarizeFeedPolls(sources, now)).toEqual({
      total: 7,
      ok: 3,
      issues: 3,
      never: 1,
      gaps: 3,
    })
    expect(summarizeFeedPolls([], now)).toEqual({
      total: 0,
      ok: 0,
      issues: 0,
      never: 0,
      gaps: 0,
    })
  })

  it("lists failures, cooldowns, gaps and unpolled feeds before healthy ones", () => {
    expect(
      sortFeedPollSources(sources, now).map(item => item.source_name)
    ).toEqual(["Also gap", "Fail", "Cool", "Gap", "Never", "Alpha", "Bravo"])
  })
})
