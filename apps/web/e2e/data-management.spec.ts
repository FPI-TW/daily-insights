import type { JobRun, RoutineRun } from "@daily-insights/api-client"
import { expect, test } from "@playwright/test"
import { authenticateAs, resetMockApi } from "./helpers"

const refresh: JobRun = {
  id: "10000000-0000-4000-8000-000000000001",
  routine_run_id: null,
  job_key: "global_macro_refresh",
  kind: "function",
  trigger: "manual",
  edition_date: "2026-09-07",
  deadline_at: null,
  status: "succeeded",
  requested_by_user_id: null,
  payload: null,
  created_at: "2026-09-07T00:00:00Z",
  started_at: "2026-09-07T00:00:00Z",
  completed_at: "2026-09-07T00:01:00Z",
  result: null,
  error: null,
  functions: [],
  depends_on: [],
  downstream_jobs: ["10000000-0000-4000-8000-000000000002"],
}
const publish: JobRun = {
  ...refresh,
  id: "10000000-0000-4000-8000-000000000002",
  job_key: "market_reports_publish",
  kind: "projection",
  status: "pending",
  started_at: null,
  completed_at: null,
  depends_on: [refresh.id],
  downstream_jobs: ["10000000-0000-4000-8000-000000000099"],
}
const dates = [
  "2026-09-08",
  "2026-09-02",
  "2026-09-03",
  "2026-09-04",
  "2026-09-05",
  "2026-09-06",
  "2026-09-07",
]
const routines: RoutineRun[] = dates.map((date, index) => ({
  id: `20000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
  routine_key: "daily_market_update_v1",
  registry_version: "e2e-v1",
  edition_date: date,
  scheduled_for: `${date}T00:00:00Z`,
  deadline_at: `${date}T02:00:00Z`,
  status: "succeeded",
  started_at: `${date}T00:00:00Z`,
  completed_at: `${date}T01:00:00Z`,
  result: null,
  created_at: `${date}T00:00:00Z`,
  jobs: [],
}))

for (const viewport of [
  { width: 1280, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`zh-hant data management at ${viewport.width}px`, async ({
    page,
    context,
    request,
  }, testInfo) => {
    await resetMockApi(request)
    await authenticateAs(context, "admin")
    await page.setViewportSize(viewport)
    await page.route("**/api/admin/orchestration/catalog", route =>
      route.fulfill({
        json: {
          taipei_date: "2026-09-07",
          registry_version: "e2e-v1",
          registry_digest: "e2e-registry",
          providers: [],
          functions: [],
          jobs: [],
          routine_key: "daily_market_update_v1",
          manual_market_jobs: [
            "global_macro_refresh",
            "us_equity_refresh",
            "tw_equity_refresh",
          ],
          features: {},
        },
      })
    )
    await page.route("**/api/admin/orchestration/job-runs?*", route =>
      route.fulfill({
        json: {
          items: [publish, refresh],
          page: 1,
          page_size: 20,
          total: 2,
          has_more: false,
        },
      })
    )
    await page.route("**/api/admin/orchestration/routine-runs?*", route =>
      route.fulfill({
        json: {
          items: routines,
          page: 1,
          page_size: 20,
          total: routines.length,
          has_more: false,
        },
      })
    )
    await page.goto("/zh-hant/admin/data-management")
    await expect(
      page.getByRole("heading", { name: "資料管理", exact: true })
    ).toBeVisible()
    const routineSection = page.locator("section").filter({
      has: page.getByRole("heading", {
        name: "每日更新 Routine",
        exact: true,
      }),
    })
    await expect(routineSection.locator("summary")).toHaveCount(5)
    await expect(routineSection.locator("summary").first()).toContainText(
      "2026-09-07"
    )
    await expect(routineSection.locator("summary").last()).toContainText(
      "2026-09-03"
    )
    const latest = page.locator("section").filter({
      has: page.getByRole("heading", { name: "最近執行結果", exact: true }),
    })
    await expect(latest.locator("article")).toHaveCount(1)
    await expect(
      latest.getByText(
        "本頁順序依據相依關係，再依開始或排入時間排列；可同時執行。",
        { exact: true }
      )
    ).toHaveCount(0)
    await expect(latest.locator("article")).toHaveClass(
      /(?:^|\s)border-2(?:\s|$)/
    )
    for (const step of await latest.locator("details").all()) {
      await expect(step).toHaveClass(/(?:^|\s)border(?:\s|$)/)
      await expect(step).not.toHaveClass(/(?:^|\s)border-2(?:\s|$)/)
    }
    await expect(latest.locator("summary").first()).toContainText(
      "步驟 1 · 更新資料"
    )
    await expect(latest.locator("summary").last()).toContainText(
      "步驟 2 · 發布"
    )
    await expect(latest.getByText(/關聯作業未列於本頁/)).toBeVisible()
    await latest.locator("summary").last().click()
    await expect(latest.getByRole("button", { name: "取消作業" })).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    ).toBe(true)
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({
      path: testInfo.outputPath(`data-management-${viewport.width}.png`),
      fullPage: true,
    })
  })
}
