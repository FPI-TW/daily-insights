import { describe, expect, it } from "vitest"
import { formatChapterText, parseChapterText } from "./podcast-chapter-text"

describe("Podcast chapter text", () => {
  it("round-trips chapters through the line format", () => {
    const chapters = [
      { start_seconds: 0, title: "FOMC 決議" },
      { start_seconds: 130, title: "外資回補 三大權值股" },
      { start_seconds: 3725, title: "Late" },
    ]
    const text = formatChapterText(chapters)
    expect(text).toBe("0:00 FOMC 決議\n2:10 外資回補 三大權值股\n62:05 Late")
    expect(parseChapterText(text)).toEqual({ ok: true, chapters })
  })

  it("accepts hour clocks, blank lines and surrounding spaces", () => {
    expect(parseChapterText("\n  1:02:03   Hour mark  \n\n")).toEqual({
      ok: true,
      chapters: [{ start_seconds: 3723, title: "Hour mark" }],
    })
    expect(parseChapterText("")).toEqual({ ok: true, chapters: [] })
  })

  it("reports the failing line", () => {
    expect(parseChapterText("0:00 A\nno clock here")).toEqual({
      ok: false,
      line: 2,
      code: "format",
    })
    expect(parseChapterText("2:00 A\n1:00 B")).toEqual({
      ok: false,
      line: 2,
      code: "order",
    })
    expect(parseChapterText("0:00 A\n9:00 B", 490)).toEqual({
      ok: false,
      line: 2,
      code: "beyond_duration",
    })
    const many = Array.from({ length: 21 }, (_, i) => `${i}:00 C${i}`).join(
      "\n"
    )
    expect(parseChapterText(many)).toEqual({
      ok: false,
      line: 21,
      code: "too_many",
    })
  })
})
