import { expect, test } from "@playwright/test"
import type { JobRun, OrchestrationCatalog } from "@daily-insights/api-client"
import { authenticateAs, resetMockApi } from "./helpers"

const labels = {
  "zh-hant": {
    title: "新聞管理",
    loading: "正在載入新聞管理。",
    markets: "單一市場新聞重抓",
    history: "最近新聞執行結果",
    dependencies: "來源與新聞模型狀態",
    enqueue: "美國股市",
    unavailable: "每日新聞目前未啟用。",
  },
  "zh-hans": {
    title: "新闻管理",
    loading: "正在加载新闻管理。",
    markets: "单一市场新闻重抓",
    history: "最近新闻执行结果",
    dependencies: "来源与新闻模型状态",
    enqueue: "美国股市",
    unavailable: "每日新闻目前未启用。",
  },
  en: {
    title: "News management",
    loading: "Loading news management.",
    markets: "Single-market news refresh",
    history: "Latest news runs",
    dependencies: "Source and news model health",
    enqueue: "US equities",
    unavailable: "Daily news is currently unavailable.",
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
    const run: JobRun = {
      id: "10000000-0000-4000-8000-000000000076",
      routine_run_id: null,
      job_key: "news_us_equity_refresh_job",
      kind: "function",
      trigger: "manual",
      edition_date: today,
      deadline_at: null,
      status: "failed",
      requested_by_user_id: null,
      payload: null,
      created_at: timestamp,
      started_at: timestamp,
      completed_at: timestamp,
      result: null,
      error: "provider_http_402",
      functions: [],
      depends_on: [],
      downstream_jobs: [],
    }
    const catalog: OrchestrationCatalog = {
      taipei_date: today,
      registry_version: "e2e-v1",
      registry_digest: "e2e-test-registry",
      providers: [],
      functions: [],
      jobs: [],
      routine_key: "daily_update",
      manual_market_jobs: ["news_us_equity_refresh_job"],
      features: { daily_news: true },
    }
    let catalogCalls = 0
    await page.route("**/api/admin/orchestration/catalog", async route => {
      catalogCalls += 1
      await route.fulfill(
        catalogCalls <= 2
          ? { status: 503, json: { detail: "test temporarily unavailable" } }
          : { json: catalog }
      )
    })
    await page.route("**/api/admin/orchestration/job-runs?*", route =>
      route.fulfill({
        json: {
          items: [run],
          page: 1,
          page_size: 20,
          total: 1,
          has_more: false,
        },
      })
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
    let enqueueCalls = 0
    await page.route("**/api/admin/orchestration/job-runs", async route => {
      enqueueCalls += 1
      expect(route.request().method()).toBe("POST")
      expect(route.request().headers()["x-csrf-token"]).toBeTruthy()
      expect(route.request().postDataJSON()).toEqual({
        job_key: "news_us_equity_refresh_job",
      })
      await route.fulfill({
        status: 503,
        headers: { "x-request-id": "enqueue-test-request" },
        json: { detail: "test temporarily unavailable" },
      })
    })
    await page.goto(`/${locale}/admin/news-management`)
    await expect(page.getByText(text.loading, { exact: true })).toHaveCount(1)
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(
      page.getByRole("heading", { name: text.title, exact: true })
    ).toBeVisible({ timeout: 15_000 })
    expect(catalogCalls).toBe(3)
    const progress = page.getByRole("region", { name: text.markets })
    await expect(progress.getByRole("button")).toHaveCount(3)
    const history = page.getByRole("region", { name: text.history })
    await history.locator("summary").click()
    await expect(
      history.getByText("provider_http_402", { exact: true })
    ).toBeVisible()
    await page.getByText(text.dependencies, { exact: true }).click()
    await expect(page.getByText("provider:news", { exact: true })).toBeVisible()
    await page.clock.install()
    const mutationResponse = page.waitForResponse(
      response =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/admin/orchestration/job-runs"
    )
    await progress
      .getByRole("button", { name: text.enqueue, exact: true })
      .click()
    const failed = await mutationResponse
    expect(failed.status()).toBe(503)
    expect(failed.headers()["x-request-id"]).toBe("enqueue-test-request")
    await expect(page.getByRole("alert")).toContainText(text.unavailable)
    await expect(progress).toBeVisible()
    await expect(
      history.getByText("provider_http_402", { exact: true })
    ).toBeVisible()
    await page.clock.fastForward(31_000)
    await page.screenshot({
      path: testInfo.outputPath(`news-recovery-${locale}.png`),
      fullPage: true,
    })
    expect(enqueueCalls).toBe(1)
  })
}
