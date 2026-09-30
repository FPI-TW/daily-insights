import { expect, test, type Page } from "@playwright/test"
import { authenticateAs, resetMockApi } from "./helpers"

const today = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Taipei",
}).format(new Date())
const evening = `${today}T10:00:00+00:00`
const editionId = "20000000-0000-4000-8000-000000000001"

function candidate(index: number, overrides: Record<string, unknown>) {
  return {
    id: `20000000-0000-4000-8000-00000000010${index}`,
    stage: "reviewed",
    drop_reason: null,
    headline: `候選新聞 ${index}`,
    source_name: "中央社",
    hostname: "www.cna.com.tw",
    url: `https://www.cna.com.tw/news/${index}`,
    seen_at: evening,
    source_published_at: evening,
    ai_rank: null,
    ai_topic: null,
    ai_market: null,
    ai_importance: null,
    ai_event_key: null,
    discovered_via: "live",
    screen_rank: index,
    screen_score: 4,
    item_id: null,
    publish_run_id: null,
    publish_requested_at: null,
    publish_error: null,
    ...overrides,
  }
}

const pollSource = {
  source_key: "a".repeat(64),
  source_name: "The Guardian",
  hostname: "www.theguardian.com",
  feed_url: "https://content.guardianapis.com/search?section=business",
  registered: true,
  poll_group: "normal",
  markets: ["global", "us_equity"],
  last_attempt_at: evening,
  last_success_at: `${today}T08:00:00+00:00`,
  last_status: 429,
  last_count: 12,
  last_error_code: "rate_limited",
  cooldown_until: `${today}T10:15:00+00:00`,
  consecutive_failures: 2,
  last_gap_minutes: 45,
  gap_count: 3,
  gap_count_since: today,
}

type CollectionState = "full" | "empty" | "error" | "loading"

async function routeNewsAdmin(page: Page, collection: CollectionState) {
  await page.route("**/api/admin/orchestration/catalog", route =>
    route.fulfill({
      json: {
        taipei_date: today,
        registry_version: "test",
        registry_digest: "test",
        providers: [],
        functions: [],
        jobs: [],
        routine_key: "daily",
        manual_market_jobs: [],
        features: { daily_news: true },
      },
    })
  )
  await page.route("**/api/admin/orchestration/job-runs?*", route =>
    route.fulfill({
      json: { items: [], page: 1, page_size: 20, total: 0, has_more: false },
    })
  )
  await page.route("**/api/admin/news/recovery", route =>
    route.fulfill({ json: { dependencies: [] } })
  )
  await page.route("**/api/admin/news/editions**", route =>
    route.fulfill({
      json: {
        edition_date: today,
        editions: [
          {
            market_code: "global",
            edition: {
              id: editionId,
              revision: 1,
              status: "partial",
              generated_at: `${today}T00:05:00+00:00`,
              prompt_version: "screen-v1:abc+selection-v7:def",
              target_items: 5,
              counts: {
                discovered: 0,
                fetch_failed: 1,
                unused: 1,
                reviewed: 2,
                prepared: 0,
                dropped: 0,
                published: 0,
                screened_out: 1,
                hidden: 0,
              },
            },
            items: [],
            candidates: [
              candidate(1, { discovered_via: "collected", screen_score: 5 }),
              candidate(2, { discovered_via: "both" }),
              candidate(3, {
                stage: "unused",
                headline:
                  "Taiwan Semiconductor Manufacturing Co. raises full-year capital expenditure guidance as advanced packaging demand outpaces supply",
              }),
              candidate(4, {
                stage: "screened_out",
                discovered_via: "collected",
                screen_rank: null,
                screen_score: null,
              }),
            ],
            pool: { live: 1, collected: 2, both: 1, screen_selected: 3 },
          },
          ...["tw_equity", "us_equity"].map(market_code => ({
            market_code,
            edition: null,
            items: [],
            candidates: [],
            pool: { live: 0, collected: 0, both: 0, screen_selected: 0 },
          })),
        ],
      },
    })
  )
  await page.route("**/api/admin/news/collection", async route => {
    if (collection === "loading") return
    if (collection === "error") {
      await route.fulfill({
        status: 500,
        headers: { "x-request-id": "collection-e2e-request" },
        json: { detail: "test failure" },
      })
      return
    }
    await route.fulfill({
      json: {
        as_of: evening,
        sources: collection === "empty" ? [] : manySources,
      },
    })
  })
}

// Enough feeds to scroll, with long names, URLs and error codes.
const manySources = [
  pollSource,
  {
    ...pollSource,
    source_key: "b".repeat(64),
    source_name: "經濟日報",
    hostname: "money.udn.com",
    feed_url: "https://money.udn.com/rssfeed/news/1001/5591",
    poll_group: "fast",
    markets: ["tw_equity"],
    last_success_at: evening,
    last_status: 200,
    last_count: 20,
    last_error_code: null,
    cooldown_until: null,
    consecutive_failures: 0,
    last_gap_minutes: null,
    gap_count: 0,
  },
  {
    ...pollSource,
    source_key: "c".repeat(64),
    source_name:
      "PR Newswire Financial Services and Investing Press Releases (United States)",
    hostname: "www.prnewswire.com",
    feed_url:
      "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    poll_group: "flash",
    markets: ["global", "us_equity"],
    last_success_at: `${today}T06:00:00+00:00`,
    last_status: 503,
    last_count: 10,
    last_error_code: "feed_http_503_service_unavailable_upstream_timeout",
    cooldown_until: null,
    consecutive_failures: 4,
    last_gap_minutes: 180,
    gap_count: 12,
  },
  {
    ...pollSource,
    source_key: "d".repeat(64),
    source_name: "retired.example",
    hostname: "retired.example",
    feed_url: "https://retired.example/rss.xml",
    registered: false,
    poll_group: null,
    markets: [],
    last_attempt_at: null,
    last_success_at: null,
    last_status: null,
    last_count: null,
    last_error_code: null,
    cooldown_until: null,
    consecutive_failures: 0,
    last_gap_minutes: null,
    gap_count: 0,
    gap_count_since: null,
  },
  ...Array.from({ length: 8 }, (_, index) => ({
    ...pollSource,
    source_key: String(index).repeat(64),
    source_name: `鉅亨網 ${index + 1}`,
    hostname: "news.cnyes.com",
    feed_url: `https://news.cnyes.com/rss/v1/news/category/tw_stock_${index}`,
    poll_group: "fast",
    markets: ["tw_equity"],
    last_success_at: evening,
    last_status: index % 3 === 0 ? 304 : 200,
    last_count: 20 - index,
    last_error_code: null,
    cooldown_until: null,
    consecutive_failures: 0,
    last_gap_minutes: index === 2 ? 30 : null,
    gap_count: index === 2 ? 1 : 0,
  })),
]

const text = {
  "zh-hant": {
    region: /隔夜蒐集來源/,
    current: "目前狀態",
    cooling: "冷卻中",
    pool: "候選來源",
    shortlisted: "初篩入選 3",
    loading: "正在載入來源輪詢狀態。",
    empty: "尚無輪詢紀錄",
  },
  en: {
    region: /Overnight collection sources/,
    current: "Current status",
    cooling: "Cooling down",
    pool: "Candidate sources",
    shortlisted: "Shortlisted 3",
    loading: "Loading feed polling status.",
    empty: "No polls recorded yet",
  },
} as const

async function openNewsManagement(
  page: Page,
  locale: keyof typeof text,
  scheme: "light" | "dark",
  collection: CollectionState
) {
  // The stored theme mode wins over the media query; the app defaults to light.
  await page.addInitScript(mode => {
    window.localStorage.setItem("theme", mode)
  }, scheme)
  await routeNewsAdmin(page, collection)
  await page.goto(`/${locale}/admin/news-management`)
  await expect(page.locator("html")).toHaveAttribute("data-theme", scheme)
  const region = page.getByRole("region", { name: text[locale].region })
  await expect(region).toBeVisible({ timeout: 15_000 })
  return region
}

async function expectNoPageOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true)
}

for (const locale of ["zh-hant", "en"] as const) {
  for (const scheme of ["light", "dark"] as const) {
    test(`${locale} ${scheme}: collection status and screen results fit desktop and phone`, async ({
      page,
      context,
      request,
    }, testInfo) => {
      await resetMockApi(request)
      await authenticateAs(context, "admin")
      await page.setViewportSize({ width: 1440, height: 900 })
      const region = await openNewsManagement(page, locale, scheme, "full")
      const labels = text[locale]
      await expect(region.getByText(labels.current)).toBeVisible()
      await expect(
        region.getByRole("rowheader", { name: /The Guardian/ })
      ).toBeVisible()
      await expect(region.getByText(labels.cooling)).toBeVisible()
      await expect(page.getByRole("list", { name: labels.pool })).toContainText(
        labels.shortlisted
      )
      await expectNoPageOverflow(page)
      await page.screenshot({
        path: testInfo.outputPath(
          `news-collection-${locale}-${scheme}-1440.png`
        ),
        fullPage: true,
      })

      await page.setViewportSize({ width: 390, height: 900 })
      await expectNoPageOverflow(page)
      await page.screenshot({
        path: testInfo.outputPath(
          `news-collection-${locale}-${scheme}-390.png`
        ),
        fullPage: true,
      })
    })
  }
}

for (const state of ["loading", "empty", "error"] as const) {
  test(`collection status ${state} state`, async ({
    page,
    context,
    request,
  }, testInfo) => {
    await resetMockApi(request)
    await authenticateAs(context, "admin")
    await page.setViewportSize({ width: 1440, height: 900 })
    const region = await openNewsManagement(page, "zh-hant", "light", state)
    const labels = text["zh-hant"]
    if (state === "loading") {
      await expect(region.getByRole("status")).toHaveText(labels.loading)
      await expect(region.getByRole("alert")).toHaveCount(0)
    } else if (state === "empty") {
      await expect(
        region.getByText(labels.empty, { exact: false })
      ).toBeVisible()
    } else {
      await expect(region.getByRole("alert")).toContainText(
        "collection-e2e-request"
      )
    }
    await region.screenshot({
      path: testInfo.outputPath(`news-collection-${state}.png`),
    })
  })
}
