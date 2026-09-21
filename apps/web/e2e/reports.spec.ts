import { expect, test } from "@playwright/test"
import {
  authenticateAs,
  customerCredentials,
  getMockApiState,
  openHydrated,
  resetMockApi,
} from "./helpers"

test.beforeEach(async ({ context, request }) => {
  await resetMockApi(request)
  await context.clearCookies()
})

test("legal statement opens from settings without overflowing the viewport", async ({
  context,
  page,
}) => {
  await authenticateAs(context, "org_member")
  await openHydrated(page, "/zh-hant/reports", 'button[aria-label="設定"]')

  await page.getByRole("button", { name: "設定" }).click()
  await page.getByRole("button", { name: "法律聲明" }).click()

  const dialog = page.getByRole("dialog", {
    name: "法律聲明 (Legal Statement)",
  })
  await expect(dialog).toBeVisible()
  await expect(
    dialog.getByRole("heading", {
      name: "1. 使用者服務條款 (Terms of Service)",
    })
  ).toBeVisible()
  await expect(
    dialog.getByRole("heading", {
      name: "4. 綜合條款 (Miscellaneous)",
    })
  ).toBeAttached()

  await page.setViewportSize({ width: 375, height: 720 })
  await expect
    .poll(async () => {
      const box = await dialog.boundingBox()
      return box !== null && box.x >= 0 && box.width <= 375 && box.height <= 720
    })
    .toBe(true)
})

test("customer login opens reports, then a market detail without mobile overflow", async ({
  page,
}) => {
  await openHydrated(page, "/en/login", 'input[name="email"]')
  await page.getByLabel("Email").fill(customerCredentials.email)
  await page.getByLabel("Password").fill(customerCredentials.password)
  await page.getByRole("button", { name: "Sign in" }).click()
  await expect(page).toHaveURL("/en/reports")
  await page
    .getByRole("navigation", { name: "Market category navigation" })
    .getByRole("link", { name: "Global macro", exact: true })
    .click()
  await expect(page).toHaveURL("/en/reports/global_macro_bonds")
  await expect(
    page.getByRole("heading", { name: "Global macro", level: 1, exact: true })
  ).toBeVisible()
  await expect(
    page.getByRole("heading", {
      name: "Global foreign exchange price trends",
    })
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: /equities news/i })
  ).toHaveCount(0)
  await expect(
    page.getByRole("heading", { name: "Today", exact: true })
  ).toHaveCount(0)
  await expect(
    page.getByRole("heading", {
      name: "Today’s economic calendar + central bank events",
    })
  ).toHaveCount(0)
  const fxPanel = page
    .getByRole("heading", { name: "Global foreign exchange price trends" })
    .locator("xpath=ancestor::section[1]")
  await page
    .getByRole("group", { name: "Currency pair" })
    .getByRole("button", { name: "USD/JPY" })
    .click()
  await expect(
    page
      .getByRole("group", { name: "Currency pair" })
      .getByRole("button", { name: "USD/JPY" })
  ).toHaveAttribute("aria-pressed", "true")
  await expect(
    fxPanel.getByLabel("Latest observations and dates")
  ).toContainText("JPY")
  await fxPanel.getByRole("button", { name: "365 days" }).click()
  await expect(
    fxPanel.getByRole("button", { name: "365 days" })
  ).toHaveAttribute("aria-pressed", "true")
  await page.setViewportSize({ width: 375, height: 720 })
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true)
})

test("unlaunched market has a clear non-error state", async ({
  context,
  page,
}) => {
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/tw_index_derivatives")
  await expect(
    page.getByRole("heading", { name: "Report not launched yet" })
  ).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
})

test("visible market without a publication shows a non-error state", async ({
  context,
  page,
  request,
}) => {
  await resetMockApi(request, { reports: "not_generated" })
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/us_equity")

  await expect(
    page.getByText("This section has not been generated yet.", { exact: true })
  ).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await expect(
    page.getByRole("navigation", { name: "Market category navigation" })
  ).toBeVisible()
})

test("US market renders a responsive VIX chart with MACD and KD", async ({
  context,
  page,
}) => {
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/us_equity")

  await expect(
    page.getByRole("heading", { name: "Index performance", exact: true })
  ).toBeVisible()
  const newsHeading = page.getByRole("heading", {
    name: "US equities news",
    exact: true,
  })
  const viewpointHeading = page.getByRole("heading", {
    name: "Analyst viewpoint",
    exact: true,
  })
  const marketHeading = page.getByRole("heading", {
    name: "US five-index performance",
    exact: true,
  })
  await expect(newsHeading).toBeVisible()
  await expect(viewpointHeading).toBeVisible()
  const [newsBox, viewpointBox, marketBox] = await Promise.all([
    newsHeading.boundingBox(),
    viewpointHeading.boundingBox(),
    marketHeading.boundingBox(),
  ])
  expect(newsBox).not.toBeNull()
  expect(viewpointBox).not.toBeNull()
  expect(marketBox).not.toBeNull()
  expect(newsBox!.y).toBeLessThan(viewpointBox!.y)
  expect(viewpointBox!.y).toBeLessThan(marketBox!.y)
  const indexPanel = page
    .getByRole("heading", { name: "Index performance", exact: true })
    .locator("xpath=ancestor::section[1]")
  await expect(
    indexPanel.getByText(
      "Index data is temporarily unavailable. The morning report remains available."
    )
  ).toHaveCount(0)
  await expect(indexPanel.locator("canvas").first()).toBeVisible()
  const titleBox = await indexPanel
    .getByRole("heading", { name: "Index performance", exact: true })
    .boundingBox()
  const selectorBox = await indexPanel.getByRole("combobox").boundingBox()
  expect(titleBox?.y).toBeLessThan(selectorBox?.y ?? 0)
  await expect
    .poll(
      async () =>
        (await indexPanel.locator("canvas").first().boundingBox())?.height
    )
    .toBeGreaterThan(600)
  await expect(
    page.getByRole("heading", { name: "Index technicals", exact: true })
  ).toHaveCount(0)
  await expect(
    page.getByRole("heading", { name: "VIX volatility trend" })
  ).toBeVisible()
  const vixPanel = page
    .getByRole("heading", { name: "VIX volatility trend" })
    .locator("xpath=ancestor::section[1]")
  await expect
    .poll(async () => (await vixPanel.locator("canvas").boundingBox())?.height)
    .toBeGreaterThan(600)
  await expect(
    page.getByText(/20 and 30 are reference risk bands/)
  ).toBeVisible()
  await expect(page.getByText("Latest VIX:").locator("..")).toContainText(
    "32.70"
  )

  await page.setViewportSize({ width: 375, height: 720 })
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true)
})

for (const [
  locale,
  heading,
  cumulative,
  foreign,
  days60,
  indexTitle,
  bias,
  newsTitle,
  viewpointTitle,
] of [
  [
    "zh-hant",
    "三大法人每日買賣超",
    "累積",
    "外資",
    "近 60 日",
    "台灣加權指數",
    "台股乖離率",
    "台股重點新聞",
    "分析師觀點",
  ],
  [
    "zh-hans",
    "三大法人每日买卖超",
    "累积",
    "外资",
    "近 60 日",
    "台湾加权指数",
    "台股乖离率",
    "台股重点新闻",
    "分析师观点",
  ],
  [
    "en",
    "Daily institutional net buying",
    "Cumulative",
    "Foreign",
    "Last 60d",
    "Taiwan Weighted Index",
    "TAIEX bias",
    "Taiwan equities news",
    "Analyst viewpoint",
  ],
] as const) {
  test(`Taiwan institutional flows render correctly in ${locale}`, async ({
    context,
    page,
    request,
  }) => {
    await authenticateAs(context, "org_member")
    await page.setViewportSize({ width: 1024, height: 900 })
    await openHydrated(
      page,
      `/${locale}/reports/tw_equity`,
      'button[aria-pressed="true"]'
    )
    const state = await getMockApiState(request)
    const institutionalRequests = state.requests.filter(item =>
      item.path.includes("institutional")
    )
    expect(institutionalRequests).toHaveLength(2)
    expect(institutionalRequests.map(item => item.role)).toEqual([
      "org_member",
      "org_member",
    ])
    const section = page
      .getByRole("heading", { name: heading, level: 3 })
      .locator("xpath=ancestor::section[2]")
    await expect(section).toBeVisible()
    // The stock tables no longer carry a dated closing note; the latest net
    // flow figure is the panel's own "data arrived" signal.
    await expect(
      section.getByText(locale === "en" ? "Latest" : "最新", { exact: true })
    ).toBeVisible()
    await expect(section.getByRole("button", { pressed: true })).toHaveCount(3)
    await expect(
      section.getByText(locale === "en" ? "TSMC" : "台積電")
    ).toBeVisible()
    await expect(
      section.getByText(locale === "en" ? "ASUS" : "華碩")
    ).toBeVisible()
    for (const label of [cumulative, foreign, days60]) {
      const control = section.getByRole("button", { name: label, exact: true })
      await control.click()
      await expect(control).toHaveAttribute("aria-pressed", "true")
    }
    await expect(section.getByText(/layout placeholder|版面示意/)).toHaveCount(
      0
    )
    const newsHeading = page.getByRole("heading", {
      name: newsTitle,
      exact: true,
    })
    const viewpointHeading = page.getByRole("heading", {
      name: viewpointTitle,
      exact: true,
    })
    const indexHeading = page.getByRole("heading", {
      name: indexTitle,
      exact: true,
    })
    const biasHeading = page.getByRole("heading", { name: bias, exact: true })
    await expect(newsHeading).toBeVisible()
    await expect(viewpointHeading).toBeVisible()
    await expect(indexHeading).toBeVisible()
    await expect(biasHeading).toBeVisible()
    const [newsBox, viewpointBox, indexBox, biasBox, sectionBox] =
      await Promise.all([
        newsHeading.boundingBox(),
        viewpointHeading.boundingBox(),
        indexHeading.boundingBox(),
        biasHeading.boundingBox(),
        section.boundingBox(),
      ])
    for (const box of [newsBox, viewpointBox, indexBox, biasBox, sectionBox]) {
      expect(box).not.toBeNull()
    }
    expect(newsBox!.y).toBeLessThan(viewpointBox!.y)
    expect(viewpointBox!.y).toBeLessThan(indexBox!.y)
    expect(indexBox!.y).toBeLessThan(biasBox!.y)
    expect(biasBox!.y).toBeLessThan(sectionBox!.y)
    await page.setViewportSize({ width: 375, height: 720 })
    await expect
      .poll(() =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth
        )
      )
      .toBe(true)
  })
}
