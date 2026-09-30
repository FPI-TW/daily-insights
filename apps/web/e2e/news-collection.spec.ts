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
        sources: collection === "empty" ? [] : productionSources,
      },
    })
  })
}

// The production registry: display name, hostname, feed URL (credentials
// already stripped by the API), poll group and markets.
const registry: ReadonlyArray<
  readonly [string, string, string, string, readonly string[]]
> = [
  [
    "鉅亨",
    "news.cnyes.com",
    "https://news.cnyes.com/rss/v1/news/category/tw_stock",
    "fast",
    ["tw_equity"],
  ],
  [
    "鉅亨",
    "news.cnyes.com",
    "https://news.cnyes.com/rss/v1/news/category/headline",
    "fast",
    ["tw_equity"],
  ],
  [
    "鉅亨",
    "news.cnyes.com",
    "https://news.cnyes.com/rss/v1/news/category/wd_stock",
    "fast",
    ["us_equity"],
  ],
  [
    "經濟日報",
    "money.udn.com",
    "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
    "fast",
    ["tw_equity"],
  ],
  [
    "經濟日報",
    "money.udn.com",
    "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
    "fast",
    ["tw_equity"],
  ],
  [
    "中央社",
    "www.cna.com.tw",
    "https://feeds.feedburner.com/rsscna/finance",
    "fast",
    ["tw_equity"],
  ],
  [
    "ETtoday 財經",
    "finance.ettoday.net",
    "https://feeds.feedburner.com/ettoday/finance",
    "fast",
    ["tw_equity"],
  ],
  [
    "財經新報",
    "finance.technews.tw",
    "https://cdn.technews.tw/feed/",
    "fast",
    ["tw_equity"],
  ],
  [
    "自由財經",
    "ec.ltn.com.tw",
    "https://news.ltn.com.tw/rss/all.xml",
    "fast",
    ["tw_equity"],
  ],
  [
    "INSIDE",
    "www.inside.com.tw",
    "https://www.inside.com.tw/feed/rss",
    "fast",
    ["tw_equity"],
  ],
  [
    "遠見",
    "www.gvm.com.tw",
    "https://www.gvm.com.tw/rss",
    "fast",
    ["tw_equity"],
  ],
  [
    "今周刊",
    "www.businesstoday.com.tw",
    "https://www.businesstoday.com.tw/news-sitemap.xml",
    "fast",
    ["tw_equity"],
  ],
  [
    "風傳媒",
    "www.storm.mg",
    "https://www.storm.mg/feed/sitemap/news",
    "fast",
    ["tw_equity"],
  ],
  [
    "The Guardian",
    "www.theguardian.com",
    "https://www.theguardian.com/uk/business/rss",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "The Guardian",
    "www.theguardian.com",
    "https://www.theguardian.com/world/rss",
    "normal",
    ["global"],
  ],
  [
    "CNBC",
    "www.cnbc.com",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "CNBC",
    "www.cnbc.com",
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "normal",
    ["global"],
  ],
  [
    "CNBC",
    "www.cnbc.com",
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "CNBC",
    "www.cnbc.com",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "FXStreet",
    "www.fxstreet.com",
    "https://www.fxstreet.com/rss/news",
    "normal",
    ["global"],
  ],
  [
    "Al Jazeera",
    "www.aljazeera.com",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "normal",
    ["global"],
  ],
  [
    "Federal Reserve",
    "www.federalreserve.gov",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "European Central Bank",
    "www.ecb.europa.eu",
    "https://www.ecb.europa.eu/rss/press.html",
    "normal",
    ["global"],
  ],
  [
    "TheStreet",
    "www.thestreet.com",
    "https://www.thestreet.com/.rss/feed/a4a58455-5a41-4dfa-899c-86c49b653ed8.xml",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "City A.M.",
    "www.cityam.com",
    "https://www.cityam.com/feed/",
    "normal",
    ["global"],
  ],
  [
    "GlobeNewswire",
    "www.globenewswire.com",
    "https://www.globenewswire.com/RssFeed/subjectcode/13-Earnings%20Releases%20and%20Operating%20Results/feedTitle/GlobeNewswire%20-%20Earnings%20Releases%20and%20Operating%20Results",
    "flash",
    ["global", "us_equity"],
  ],
  [
    "GlobeNewswire",
    "www.globenewswire.com",
    "https://www.globenewswire.com/RssFeed/subjectcode/27-Mergers%20and%20Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20and%20Acquisitions",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "PR Newswire",
    "www.prnewswire.com",
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "The Guardian",
    "www.theguardian.com",
    "https://content.guardianapis.com/search?section=business&order-by=newest&page-size=50&show-fields=bodyText",
    "normal",
    ["global", "us_equity"],
  ],
  [
    "The Guardian",
    "www.theguardian.com",
    "https://content.guardianapis.com/search?section=world&order-by=newest&page-size=50&show-fields=bodyText",
    "normal",
    ["global"],
  ],
  [
    "The Guardian",
    "www.theguardian.com",
    "https://content.guardianapis.com/search?section=politics&order-by=newest&page-size=50&show-fields=bodyText",
    "normal",
    ["global"],
  ],
  [
    "SEC EDGAR",
    "www.sec.gov",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom",
    "flash",
    ["us_equity"],
  ],
]

// Mostly healthy, as on a normal night; a few feeds cool down, fail, show
// gaps or have not been polled yet.
const trouble: Record<number, Record<string, unknown>> = {
  11: {
    last_success_at: `${today}T08:00:00+00:00`,
    last_status: 503,
    last_error_code: "feed_http_503",
    consecutive_failures: 1,
  },
  20: {
    last_attempt_at: null,
    last_success_at: null,
    last_status: null,
    last_count: null,
  },
  23: { last_count: 10, last_gap_minutes: 95, gap_count: 2 },
  27: { last_count: 10, last_gap_minutes: 180, gap_count: 3 },
  28: {
    last_success_at: `${today}T08:00:00+00:00`,
    last_status: 429,
    last_error_code: "rate_limited",
    cooldown_until: `${today}T10:15:00+00:00`,
    consecutive_failures: 2,
  },
  31: {
    last_success_at: `${today}T06:00:00+00:00`,
    last_status: 403,
    last_error_code: "feed_http_403_forbidden_missing_contact_email",
    consecutive_failures: 4,
    last_gap_minutes: 60,
    gap_count: 1,
  },
}

const productionSources = registry.map(
  ([source_name, hostname, feed_url, poll_group, markets], index) => ({
    source_key: index.toString(16).padStart(64, "0"),
    source_name,
    hostname,
    feed_url,
    registered: true,
    poll_group,
    markets,
    last_attempt_at: evening,
    last_success_at: evening,
    last_status: index % 4 === 0 ? 304 : 200,
    last_count: index % 4 === 0 ? 0 : 20 - (index % 7),
    last_error_code: null,
    cooldown_until: null,
    consecutive_failures: 0,
    last_gap_minutes: null,
    gap_count: 0,
    gap_count_since: today,
    ...trouble[index],
  })
)

const text = {
  "zh-hant": {
    summary: "隔夜蒐集來源",
    health: ["來源 32", "正常 28", "冷卻／失敗 3", "尚未輪詢 1", "當晚缺口 6"],
    pool: "候選來源",
    shortlisted: "初篩入選 3",
    loading: "正在載入來源輪詢狀態。",
    empty: "尚無輪詢紀錄。",
    firstRow: "SEC EDGAR",
  },
  en: {
    summary: "Overnight collection sources",
    health: [
      "Sources 32",
      "OK 28",
      "Cooling / failing 3",
      "Not polled yet 1",
      "Gaps tonight 6",
    ],
    pool: "Candidate sources",
    shortlisted: "Shortlisted 3",
    loading: "Loading feed polling status.",
    empty: "No polls recorded yet.",
    firstRow: "SEC EDGAR",
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
  const summary = page.locator("summary", { hasText: text[locale].summary })
  await expect(summary).toBeVisible({ timeout: 15_000 })
  return { summary, panel: page.locator("details", { has: summary }) }
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

// No candidate table header may wrap onto a third line.
async function expectCompactCandidateHeaders(page: Page) {
  const lines = await page
    .locator("table thead th")
    .filter({ visible: true })
    .evaluateAll(headers =>
      headers.map(header => {
        const style = getComputedStyle(header)
        const padding =
          parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
        return Math.round(
          (header.clientHeight - padding) / parseFloat(style.lineHeight)
        )
      })
    )
  expect(Math.max(...lines)).toBeLessThanOrEqual(2)
}

for (const locale of ["zh-hant", "en"] as const) {
  for (const scheme of ["light", "dark"] as const) {
    test(`${locale} ${scheme}: collapsed collection summary keeps curation near the top`, async ({
      page,
      context,
      request,
    }, testInfo) => {
      await resetMockApi(request)
      await authenticateAs(context, "admin")
      await page.setViewportSize({ width: 1440, height: 900 })
      const labels = text[locale]
      const { summary, panel } = await openNewsManagement(
        page,
        locale,
        scheme,
        "full"
      )
      await expect(panel).not.toHaveAttribute("open", "")
      for (const count of labels.health) {
        await expect(summary.getByText(count, { exact: true })).toBeVisible()
      }
      await expect(page.getByRole("list", { name: labels.pool })).toContainText(
        labels.shortlisted
      )
      await expectCompactCandidateHeaders(page)
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

for (const [locale, scheme] of [
  ["zh-hant", "light"],
  ["en", "dark"],
] as const) {
  test(`${locale} ${scheme}: expanded collection lists troubled feeds first`, async ({
    page,
    context,
    request,
  }, testInfo) => {
    await resetMockApi(request)
    await authenticateAs(context, "admin")
    await page.setViewportSize({ width: 1440, height: 900 })
    const { summary, panel } = await openNewsManagement(
      page,
      locale,
      scheme,
      "full"
    )
    await summary.click()
    await expect(panel).toHaveAttribute("open", "")
    const rows = panel.getByRole("rowheader")
    await expect(rows).toHaveCount(32)
    await expect(rows.first()).toContainText(text[locale].firstRow)
    // Feed URLs stay on one truncated line with the full URL on hover.
    const url = rows.filter({ hasText: "PR Newswire" }).locator("span[title]")
    await expect(url).toHaveCSS("text-overflow", "ellipsis")
    expect(await url.evaluate(node => node.getClientRects().length)).toBe(1)
    await expectNoPageOverflow(page)
    // Full page: an element shot would scroll the sticky header over the panel.
    await page.screenshot({
      path: testInfo.outputPath(
        `news-collection-${locale}-${scheme}-expanded-1440.png`
      ),
      fullPage: true,
    })

    await page.setViewportSize({ width: 390, height: 900 })
    await expectNoPageOverflow(page)
    expect(await url.evaluate(node => node.getClientRects().length)).toBe(1)
    // Full page: an element shot would scroll the sticky header over the panel.
    await page.screenshot({
      path: testInfo.outputPath(
        `news-collection-${locale}-${scheme}-expanded-390.png`
      ),
      fullPage: true,
    })
  })
}

for (const state of ["loading", "empty", "error"] as const) {
  test(`collection status ${state} state, expanded`, async ({
    page,
    context,
    request,
  }, testInfo) => {
    await resetMockApi(request)
    await authenticateAs(context, "admin")
    await page.setViewportSize({ width: 1440, height: 900 })
    const labels = text["zh-hant"]
    const { summary, panel } = await openNewsManagement(
      page,
      "zh-hant",
      "light",
      state
    )
    if (state === "loading") {
      await expect(summary.getByRole("status")).toHaveText(labels.loading)
    } else if (state === "empty") {
      await expect(
        summary.getByText("尚無輪詢紀錄", { exact: true })
      ).toBeVisible()
    } else {
      await expect(summary.getByText("無法載入", { exact: true })).toBeVisible()
    }
    await summary.click()
    if (state === "loading") {
      await expect(panel.getByRole("alert")).toHaveCount(0)
    } else if (state === "empty") {
      await expect(panel.getByText(labels.empty)).toBeVisible()
    } else {
      await expect(panel.getByRole("alert")).toContainText(
        "collection-e2e-request"
      )
    }
    await panel.screenshot({
      path: testInfo.outputPath(`news-collection-${state}.png`),
    })
  })
}
