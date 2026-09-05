import { describe, expect, it } from "vitest"
import {
  chapterSegments,
  clampPosition,
  clockTime,
  currentChapterIndex,
  dotDate,
  formatSpeed,
  listeningStatus,
  matchesFilter,
  monthDayLabel,
  nextPlaybackSpeed,
  releaseTimeLabel,
  waveformHeights,
  weekdayLabel,
} from "./podcast-episode"

describe("Podcast episode helpers", () => {
  it("formats clock times and dotted dates", () => {
    expect(clockTime(0)).toBe("0:00")
    expect(clockTime(95.8)).toBe("1:35")
    expect(clockTime(490)).toBe("8:10")
    expect(clockTime(Number.NaN)).toBe("0:00")
    expect(dotDate("2026-09-03")).toBe("2026.09.03")
  })

  it("labels weekdays and month-days per locale in UTC", () => {
    expect(weekdayLabel("2026-09-03", "zh-hant")).toBe("週四")
    expect(weekdayLabel("2026-09-03", "en")).toBe("Thu")
    expect(monthDayLabel("2026-09-02", "zh-hant")).toBe("9月2日")
    expect(monthDayLabel("2026-09-02", "en")).toBe("Sep 2")
  })

  it("formats the release time in the given zone", () => {
    expect(
      releaseTimeLabel("2026-09-02T23:30:00Z", "zh-hant", "Asia/Taipei")
    ).toBe("07:30")
    expect(releaseTimeLabel("2026-09-02T23:30:00Z", "en", "UTC")).toBe("23:30")
    expect(releaseTimeLabel("not-a-date", "en")).toBeNull()
  })

  it("cycles playback speed and formats it", () => {
    expect(nextPlaybackSpeed(1)).toBe(1.25)
    expect(nextPlaybackSpeed(2)).toBe(1)
    expect(formatSpeed(1)).toBe("1.0×")
    expect(formatSpeed(1.25)).toBe("1.25×")
  })

  it("builds a stable waveform inside the 8–50px range", () => {
    const heights = waveformHeights(490)
    expect(heights).toHaveLength(64)
    expect(heights).toEqual(waveformHeights(490))
    for (const height of heights) {
      expect(height).toBeGreaterThanOrEqual(8)
      expect(height).toBeLessThanOrEqual(50)
    }
    expect(waveformHeights(490, "line").every(height => height === 4)).toBe(
      true
    )
  })

  it("derives chapter lengths and the current chapter", () => {
    const segments = chapterSegments(
      [
        { start_seconds: 130, title: "B" },
        { start_seconds: 0, title: "A" },
        { start_seconds: 410, title: "C" },
      ],
      490
    )
    expect(segments.map(segment => segment.lengthSeconds)).toEqual([
      130, 280, 80,
    ])
    expect(currentChapterIndex(segments, 0)).toBe(0)
    expect(currentChapterIndex(segments, 129)).toBe(0)
    expect(currentChapterIndex(segments, 130)).toBe(1)
    expect(currentChapterIndex(segments, 480)).toBe(2)
  })

  it("classifies listening status and filters", () => {
    expect(listeningStatus(0, 500)).toEqual({ kind: "unheard" })
    expect(listeningStatus(290, 500)).toEqual({ kind: "partial", percent: 58 })
    expect(listeningStatus(499.5, 500)).toEqual({ kind: "finished" })
    expect(listeningStatus(1, 500)).toEqual({ kind: "partial", percent: 1 })
    expect(listeningStatus(30, null)).toEqual({ kind: "partial", percent: 0 })
    expect(matchesFilter("unheard", { kind: "unheard" })).toBe(true)
    expect(matchesFilter("heard", { kind: "unheard" })).toBe(false)
    expect(matchesFilter("heard", { kind: "finished" })).toBe(true)
    expect(matchesFilter("all", { kind: "finished" })).toBe(true)
  })

  it("clamps positions to the duration when known", () => {
    expect(clampPosition(-5, 100)).toBe(0)
    expect(clampPosition(120, 100)).toBe(100)
    expect(clampPosition(120, Number.NaN)).toBe(120)
  })
})
