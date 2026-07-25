import { describe, expect, it } from "vitest"
import { podcastProgressKey, restoredPosition } from "./podcast-progress"

describe("Podcast playback progress", () => {
  it("isolates progress by user, episode, and resolved locale", () => {
    expect(podcastProgressKey("user-1", "episode-1", "zh-hant")).toBe(
      "daily-insights:podcast-progress:v1:user-1:episode-1:zh-hant"
    )
    expect(podcastProgressKey("user-1", "episode-1", "en")).not.toBe(
      podcastProgressKey("user-1", "episode-1", "zh-hant")
    )
  })

  it("clamps progress to the current duration", () => {
    expect(
      restoredPosition(
        JSON.stringify({
          positionSeconds: 200,
          durationSeconds: 240,
          updatedAt: "2026-07-24T12:00:00.000Z",
        }),
        120
      )
    ).toBe(119)
  })

  it("ignores corrupt or invalid storage", () => {
    expect(restoredPosition("{broken", 120)).toBeNull()
    expect(
      restoredPosition(
        JSON.stringify({
          positionSeconds: -1,
          durationSeconds: 120,
          updatedAt: "not-a-date",
        }),
        120
      )
    ).toBeNull()
  })
})
