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
    .getByRole("link", { name: "US macro & bonds" })
    .click()
  await expect(page).toHaveURL("/en/reports/global_macro_bonds")
  await expect(
    page.getByRole("heading", { name: "US macro & bonds", level: 1 })
  ).toBeVisible()
  await expect(
    page.getByRole("heading", {
      name: "Global foreign exchange price trends",
    })
  ).toBeVisible()
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

test("US market renders a dedicated responsive VIX chart", async ({
  context,
  page,
}) => {
  await authenticateAs(context, "org_member")
  await page.goto("/en/reports/us_equity")

  await expect(
    page.getByRole("heading", { name: "VIX volatility trend" })
  ).toBeVisible()
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

for (const [locale, heading, cumulative, foreign, days60] of [
  ["zh-hant", "三大法人", "累積", "外資", "近 60 日"],
  ["zh-hans", "三大法人", "累积", "外资", "近 60 日"],
  ["en", "Institutional flows", "Cumulative", "Foreign", "Last 60d"],
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
      .getByRole("heading", { name: heading, level: 2 })
      .locator("xpath=ancestor::section[1]")
    await expect(section).toBeVisible()
    await expect(section.getByText("2026-09-04").first()).toBeVisible()
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
