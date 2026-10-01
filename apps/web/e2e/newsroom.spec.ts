import { expect, test } from "@playwright/test"
import { authenticateAs, openHydrated, resetMockApi } from "./helpers"

test.beforeEach(async ({ context, request }) => {
  await resetMockApi(request)
  await context.clearCookies()
  await authenticateAs(context, "org_member")
})

test("home page shows today's key news with why it matters and sources", async ({
  page,
}) => {
  await openHydrated(page, "/en/reports", "#daily-news-title")

  const news = page.locator("section", {
    has: page.getByRole("heading", { name: "Today’s major news" }),
  })
  const story = news.getByRole("article")
  await expect(
    story.getByRole("heading", {
      name: "Markets respond to the latest economic signals",
    })
  ).toBeVisible()
  await expect(
    story.getByRole("heading", { name: "Why it matters" })
  ).toBeVisible()
  await expect(
    story.getByText("Shifting rate expectations move equity valuations.")
  ).toBeVisible()
  // Today's edition carries no date notice.
  await expect(news.getByText(/being prepared/)).toHaveCount(0)

  const source = story.getByRole("link", { name: "Example Wire" })
  await expect(source).toBeHidden()
  await story.getByText("2 sources").click()
  await expect(source).toBeVisible()
  await expect(source).toHaveAttribute("rel", "noopener noreferrer")

  await story
    .getByRole("list", { name: "Related markets" })
    .getByRole("link", { name: "S&P 500 Index" })
    .click()
  await expect(page).toHaveURL("/en/reports/us_equity")
  await expect(
    page.getByRole("heading", { name: "US equities news", exact: true })
  ).toBeVisible()
})

for (const [locale, title, notice] of [
  ["zh-hant", "台股重點新聞", "今日新聞準備中，以下為 8月29日 內容"],
  ["zh-hans", "台股重点新闻", "今日新闻准备中，以下为 8月29日 内容"],
  [
    "en",
    "Taiwan equities news",
    "Today’s news is being prepared. Showing August 29.",
  ],
] as const) {
  test(`market page labels an earlier edition with its date in ${locale}`, async ({
    page,
  }) => {
    await openHydrated(
      page,
      `/${locale}/reports/tw_equity`,
      "#daily-news-title"
    )

    const heading = page.getByRole("heading", { name: title, exact: true })
    await expect(heading).toBeVisible()
    await expect(
      page.locator("#daily-news-title + p", { hasText: notice })
    ).toBeVisible()
  })
}
