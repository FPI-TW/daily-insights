import { describe, expect, it } from "vitest"
import { feedPollHealth } from "./news-collection"

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
