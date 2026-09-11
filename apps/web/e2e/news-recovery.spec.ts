import { expect, test } from "@playwright/test"
import type { DataManagementRun } from "@daily-insights/api-client"
import { authenticateAs, resetMockApi } from "./helpers"

const labels = {
  "zh-hant": {
    title: "新聞管理",
    loading: "正在載入新聞管理。",
    markets: "各市場執行進度",
    history: "最近新聞執行結果",
    dependencies: "來源與新聞模型狀態",
    resume: "恢復作業／試做模型",
  },
  "zh-hans": {
    title: "新闻管理",
    loading: "正在加载新闻管理。",
    markets: "各市场执行进度",
    history: "最近新闻执行结果",
    dependencies: "来源与新闻模型状态",
    resume: "恢复作业／试做模型",
  },
  en: {
    title: "News management",
    loading: "Loading news management.",
    markets: "Progress by market",
    history: "Latest news runs",
    dependencies: "Source and news model health",
    resume: "Resume / probe model",
  },
} as const

for (const locale of ["zh-hant", "zh-hans", "en"] as const) {
  test(`${locale}: recovery status survives transient GET failures and never retries POST`, async ({
    page,
    context,
    request,
  }, testInfo) => {
    await resetMockApi(request)
    await authenticateAs(context, "admin")
    const text = labels[locale]
    const today = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Taipei",
    }).format(new Date())
    const timestamp = `${today}T08:00:00+08:00`
    const failure = {
      code: "provider_http_402",
      stage: "summary",
      action: "block",
      scope: "provider:news",
      http_status: 402,
      retry_after: null,
      candidate_id: "test-candidate",
      locale: "en",
      request_id: "model-test-request",
    } as const
    const run: DataManagementRun = {
      id: "10000000-0000-4000-8000-000000000076",
      operation: "news_market",
      market_code: "us_equity",
      edition_date: today,
      status: "failed",
      requested_by_user_id: null,
      created_at: timestamp,
      started_at: timestamp,
      completed_at: timestamp,
      scheduled_for: timestamp,
      heartbeat_at: timestamp,
      result: null,
      error: "provider_http_402",
      news: {
        us_equity: {
          id: "workflow-test",
          state: "needs_attention",
          stage: "summary",
          progress: { discovered: 12, summary: 2, published: 0 },
          failures: [failure],
          attempt: 2,
          next_retry_at: null,
          publication: "technical_degradation",
        },
      },
    }
    let catalogCalls = 0
    await page.route("**/api/admin/data-management/catalog", async route => {
      catalogCalls += 1
      await route.fulfill(
        catalogCalls <= 2
          ? { status: 503, json: { detail: "test temporarily unavailable" } }
          : {
              json: {
                taipei_date: today,
                morning_reports_enabled: false,
                yfinance_enabled: false,
                twse_enabled: false,
                markets: ["global_macro_bonds", "crypto", "us_equity"],
                daily_news_enabled: true,
                news_markets: ["global", "tw_equity", "us_equity"],
                macro_dashboard_enabled: false,
              },
            }
      )
    })
    await page.route("**/api/admin/data-management/runs?*", route =>
      route.fulfill({ json: { items: [run] } })
    )
    await page.route("**/api/admin/news/editions**", route =>
      route.fulfill({
        json: {
          edition_date: today,
          editions: ["global", "tw_equity", "us_equity"].map(market_code => ({
            market_code,
            edition: null,
            items: [],
            candidates: [],
          })),
        },
      })
    )
    await page.route("**/api/admin/news/recovery", route =>
      route.fulfill({
        json: {
          dependencies: [
            {
              scope: "provider:news",
              state: "blocked",
              failure,
              available_at: null,
              newest_article_at: null,
              updated_at: timestamp,
            },
          ],
        },
      })
    )
    let resumeCalls = 0
    await page.route(
      `**/api/admin/data-management/runs/${run.id}/resume`,
      async route => {
        resumeCalls += 1
        expect(route.request().method()).toBe("POST")
        expect(route.request().headers()["x-csrf-token"]).toBeTruthy()
        expect(route.request().postDataJSON()).toEqual({
          resume_provider: true,
        })
        await route.fulfill({
          status: 503,
          headers: { "x-request-id": "resume-test-request" },
          json: { detail: "test temporarily unavailable" },
        })
      }
    )
    await page.goto(`/${locale}/admin/news-management`)
    await expect(page.getByText(text.loading, { exact: true })).toHaveCount(1)
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(
      page.getByRole("heading", { name: text.title, exact: true })
    ).toBeVisible({ timeout: 15_000 })
    expect(catalogCalls).toBe(3)
    const progress = page.getByRole("region", { name: text.markets })
    await expect(progress.locator("article")).toHaveCount(3)
    await expect(
      progress.getByText("provider_http_402", { exact: false })
    ).toBeVisible()
    await page.getByText(text.dependencies, { exact: true }).click()
    await expect(page.getByText("provider:news", { exact: true })).toBeVisible()
    const history = page.getByRole("region", { name: text.history })
    await history.locator("summary").click()
    await page.clock.install()
    await history
      .getByRole("button", { name: text.resume, exact: true })
      .click()
    await expect(page.getByRole("alert")).toContainText("resume-test-request")
    await expect(progress).toBeVisible()
    await page.clock.fastForward(31_000)
    await page.screenshot({
      path: testInfo.outputPath(`news-recovery-${locale}.png`),
      fullPage: true,
    })
    expect(resumeCalls).toBe(1)
  })
}
